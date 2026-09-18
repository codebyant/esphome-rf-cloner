#include "command_store.h"

#include <cstring>

namespace esphome::rf_cloner {

namespace {

// On-flash layout:
//
//   header  (10 bytes, fixed)     magic, format_version, slot_count, index_bytes, crc
//   index   (slot_count*40 + 2)   one StoredSlot per slot, then a crc over the slot array
//   payload (pulse_count * 4)     one record per occupied slot, native-endian int32 timings
//
// Split this way because ESPHome's preference load() only succeeds on an exact length match: the
// fixed-size header must be read first to learn the lengths of the index and each payload.
// Padding slots to max_pulses instead would cost about 1 kB each and exhaust the ~20 kB NVS
// budget well before the slot count did.

struct StoredHeader {
  uint16_t magic;
  uint8_t format_version;
  uint8_t slot_count;
  uint16_t index_bytes;
  uint16_t reserved;
  uint16_t crc16;  // over the preceding 8 bytes
} __attribute__((packed));

struct StoredSlot {
  char name[NAME_BUF_LEN];
  uint16_t pulse_count;
  uint16_t repeat_times;
  uint32_t gap_us;
  uint32_t frequency_hz;
  uint8_t modulation;
  uint8_t flags;
  uint16_t payload_crc16;
} __attribute__((packed));

static_assert(sizeof(StoredHeader) == 10, "StoredHeader layout changed; bump STORE_FORMAT_VERSION");
static_assert(sizeof(StoredSlot) == 40, "StoredSlot layout changed; bump STORE_FORMAT_VERSION");

constexpr uint32_t fnv1a_32(const char *text) {
  uint32_t hash = 2166136261UL;
  while (*text != '\0') {
    hash ^= static_cast<uint8_t>(*text++);
    hash *= 16777619UL;
  }
  return hash;
}

constexpr uint32_t KEY_HEADER = fnv1a_32("rf_cloner/hdr/v1");
constexpr uint32_t KEY_INDEX = fnv1a_32("rf_cloner/idx/v1");
constexpr uint32_t KEY_PAYLOAD_BASE = fnv1a_32("rf_cloner/pl/v1");

uint32_t payload_key(uint8_t slot) { return KEY_PAYLOAD_BASE ^ (0x9E3779B9u * (static_cast<uint32_t>(slot) + 1u)); }

uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 0x8000u) != 0 ? static_cast<uint16_t>((crc << 1) ^ 0x1021u) : static_cast<uint16_t>(crc << 1);
    }
  }
  return crc;
}

uint16_t timings_crc(const std::vector<int32_t> &timings) {
  if (timings.empty()) {
    return 0;
  }
  return crc16_ccitt(reinterpret_cast<const uint8_t *>(timings.data()), timings.size() * sizeof(int32_t));
}

}  // namespace

const char *store_result_to_string(StoreResult result) {
  switch (result) {
    case StoreResult::OK:
      return "ok";
    case StoreResult::NAME_EMPTY:
      return "name_empty";
    case StoreResult::NAME_TOO_LONG:
      return "name_too_long";
    case StoreResult::NAME_INVALID:
      return "name_invalid";
    case StoreResult::NOT_FOUND:
      return "not_found";
    case StoreResult::STORE_FULL:
      return "store_full";
    case StoreResult::TOO_MANY_PULSES:
      return "too_many_pulses";
    case StoreResult::BUDGET_EXCEEDED:
      return "budget_exceeded";
    case StoreResult::STORAGE_ERROR:
      return "storage_error";
  }
  return "unknown";
}

StoreResult CommandStore::validate_name(const std::string &name) {
  if (name.empty()) {
    return StoreResult::NAME_EMPTY;
  }
  if (name.size() > NAME_MAX_CHARS) {
    return StoreResult::NAME_TOO_LONG;
  }
  // Names appear in Home Assistant scripts and log lines; keep the character set unambiguous.
  for (char c : name) {
    const bool allowed = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' ||
                         c == '-' || c == '.';
    if (!allowed) {
      return StoreResult::NAME_INVALID;
    }
  }
  return StoreResult::OK;
}

uint32_t CommandStore::header_key_() const { return KEY_HEADER ^ this->key_salt_; }
uint32_t CommandStore::index_key_() const { return KEY_INDEX ^ this->key_salt_; }
uint32_t CommandStore::payload_key_(uint8_t slot) const { return payload_key(slot) ^ this->key_salt_; }

void CommandStore::configure(StorageBackend *backend, uint8_t max_commands, uint16_t max_pulses,
                             uint32_t max_storage_bytes, uint32_t key_salt) {
  this->backend_ = backend;
  this->key_salt_ = key_salt;
  this->max_commands_ = max_commands;
  this->max_pulses_ = max_pulses;
  this->max_storage_bytes_ = max_storage_bytes;
  this->slots_.assign(max_commands, Command{});
}

LoadReport CommandStore::begin() {
  LoadReport report{};
  if (this->backend_ == nullptr) {
    report.header_invalid = true;
    return report;
  }

  StoredHeader header{};
  if (!this->backend_->load(this->header_key_(), reinterpret_cast<uint8_t *>(&header), sizeof(header))) {
    // No record yet; first boot.
    return report;
  }
  report.had_stored_data = true;
  report.stored_format_version = header.format_version;
  report.stored_slot_count = header.slot_count;

  const uint16_t expected_crc = crc16_ccitt(reinterpret_cast<const uint8_t *>(&header), sizeof(header) - 2);
  if (header.magic != STORE_MAGIC || header.crc16 != expected_crc) {
    report.header_invalid = true;
    return report;
  }
  if (header.format_version != STORE_FORMAT_VERSION) {
    // Refuse to reinterpret an unknown format. Flash stays intact so a downgrade can read it.
    report.version_mismatch = true;
    return report;
  }

  const size_t index_bytes = static_cast<size_t>(header.slot_count) * sizeof(StoredSlot) + 2;
  if (header.index_bytes != index_bytes || header.slot_count == 0) {
    report.header_invalid = true;
    return report;
  }

  std::vector<uint8_t> raw(index_bytes);
  if (!this->backend_->load(this->index_key_(), raw.data(), raw.size())) {
    report.header_invalid = true;
    return report;
  }
  uint16_t stored_index_crc = 0;
  std::memcpy(&stored_index_crc, raw.data() + raw.size() - 2, 2);
  if (crc16_ccitt(raw.data(), raw.size() - 2) != stored_index_crc) {
    report.header_invalid = true;
    return report;
  }

  report.slot_count_changed = header.slot_count != this->max_commands_;

  for (uint8_t i = 0; i < header.slot_count; i++) {
    StoredSlot slot{};
    std::memcpy(&slot, raw.data() + static_cast<size_t>(i) * sizeof(StoredSlot), sizeof(StoredSlot));
    slot.name[NAME_BUF_LEN - 1] = '\0';
    if (slot.name[0] == '\0' || slot.pulse_count == 0) {
      continue;  // free slot
    }
    if (i >= this->max_commands_ || slot.pulse_count > this->max_pulses_) {
      // max_commands or max_pulses was reduced in YAML; flash keeps the record until the next
      // write.
      report.dropped_corrupt++;
      continue;
    }

    std::vector<int32_t> timings(slot.pulse_count);
    if (!this->backend_->load(this->payload_key_(i), reinterpret_cast<uint8_t *>(timings.data()),
                              timings.size() * sizeof(int32_t))) {
      report.dropped_corrupt++;
      continue;
    }
    if (timings_crc(timings) != slot.payload_crc16) {
      report.dropped_corrupt++;
      continue;
    }

    Command &target = this->slots_[i];
    target.name = slot.name;
    target.timings = std::move(timings);
    target.gap_us = slot.gap_us;
    target.repeat_times = slot.repeat_times;
    target.frequency_hz = slot.frequency_hz;
    target.modulation = slot.modulation;
    report.loaded++;
  }

  return report;
}

int CommandStore::find_slot_(const std::string &name) const {
  for (size_t i = 0; i < this->slots_.size(); i++) {
    if (!this->slots_[i].name.empty() && this->slots_[i].name == name) {
      return static_cast<int>(i);
    }
  }
  return -1;
}

int CommandStore::find_free_slot_() const {
  for (size_t i = 0; i < this->slots_.size(); i++) {
    if (this->slots_[i].name.empty()) {
      return static_cast<int>(i);
    }
  }
  return -1;
}

uint32_t CommandStore::used_bytes() const {
  uint32_t total = sizeof(StoredHeader) + static_cast<uint32_t>(this->max_commands_) * sizeof(StoredSlot) + 2;
  for (const Command &command : this->slots_) {
    if (!command.name.empty()) {
      total += static_cast<uint32_t>(command.timings.size() * sizeof(int32_t));
    }
  }
  return total;
}

uint32_t CommandStore::worst_case_bytes() const {
  return sizeof(StoredHeader) + static_cast<uint32_t>(this->max_commands_) * sizeof(StoredSlot) + 2 +
         static_cast<uint32_t>(this->max_commands_) * this->max_pulses_ * sizeof(int32_t);
}

uint32_t CommandStore::projected_bytes_(int replacing_slot, uint16_t new_pulse_count) const {
  uint32_t total = sizeof(StoredHeader) + static_cast<uint32_t>(this->max_commands_) * sizeof(StoredSlot) + 2;
  for (size_t i = 0; i < this->slots_.size(); i++) {
    if (static_cast<int>(i) == replacing_slot) {
      continue;
    }
    if (!this->slots_[i].name.empty()) {
      total += static_cast<uint32_t>(this->slots_[i].timings.size() * sizeof(int32_t));
    }
  }
  return total + static_cast<uint32_t>(new_pulse_count) * sizeof(int32_t);
}

StoreResult CommandStore::put(const Command &command) {
  if (this->backend_ == nullptr) {
    return StoreResult::STORAGE_ERROR;
  }
  const StoreResult name_check = validate_name(command.name);
  if (name_check != StoreResult::OK) {
    return name_check;
  }
  if (command.timings.empty()) {
    return StoreResult::TOO_MANY_PULSES;  // an empty waveform is not storable either
  }
  if (command.timings.size() > this->max_pulses_) {
    return StoreResult::TOO_MANY_PULSES;
  }

  int slot = this->find_slot_(command.name);
  const bool overwriting = slot >= 0;
  if (!overwriting) {
    slot = this->find_free_slot_();
    if (slot < 0) {
      return StoreResult::STORE_FULL;
    }
  }

  if (this->projected_bytes_(overwriting ? slot : -1, static_cast<uint16_t>(command.timings.size())) >
      this->max_storage_bytes_) {
    return StoreResult::BUDGET_EXCEEDED;
  }

  // Retained so a failed write can be rolled back in RAM as well as on flash.
  Command previous = this->slots_[slot];
  this->slots_[slot] = command;

  // Payload first: a half-applied write is invisible until the index references it.
  if (!this->write_payload_(static_cast<uint8_t>(slot)) || !this->write_index_()) {
    this->slots_[slot] = std::move(previous);
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

bool CommandStore::get(const std::string &name, Command &out) const {
  const int slot = this->find_slot_(name);
  if (slot < 0) {
    return false;
  }
  out = this->slots_[slot];
  return true;
}

bool CommandStore::has(const std::string &name) const { return this->find_slot_(name) >= 0; }

StoreResult CommandStore::remove(const std::string &name) {
  if (this->backend_ == nullptr) {
    return StoreResult::STORAGE_ERROR;
  }
  const int slot = this->find_slot_(name);
  if (slot < 0) {
    return StoreResult::NOT_FOUND;
  }
  Command previous = std::move(this->slots_[slot]);
  this->slots_[slot] = Command{};
  // Only the index is rewritten; the orphaned payload stays on flash until the slot is reused
  // and is not counted by used_bytes().
  if (!this->write_index_()) {
    this->slots_[slot] = std::move(previous);
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

StoreResult CommandStore::clear_all() {
  if (this->backend_ == nullptr) {
    return StoreResult::STORAGE_ERROR;
  }
  std::vector<Command> previous = this->slots_;
  this->slots_.assign(this->max_commands_, Command{});
  if (!this->write_index_()) {
    this->slots_ = std::move(previous);
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

size_t CommandStore::count() const {
  size_t total = 0;
  for (const Command &command : this->slots_) {
    if (!command.name.empty()) {
      total++;
    }
  }
  return total;
}

std::vector<std::string> CommandStore::names() const {
  std::vector<std::string> result;
  for (const Command &command : this->slots_) {
    if (!command.name.empty()) {
      result.push_back(command.name);
    }
  }
  return result;
}

bool CommandStore::write_payload_(uint8_t slot) {
  const Command &command = this->slots_[slot];
  return this->backend_->save(this->payload_key_(slot), reinterpret_cast<const uint8_t *>(command.timings.data()),
                              command.timings.size() * sizeof(int32_t));
}

bool CommandStore::write_index_() {
  const size_t index_bytes = static_cast<size_t>(this->max_commands_) * sizeof(StoredSlot) + 2;
  std::vector<uint8_t> raw(index_bytes, 0);

  for (uint8_t i = 0; i < this->max_commands_; i++) {
    StoredSlot slot{};
    const Command &command = this->slots_[i];
    if (!command.name.empty()) {
      std::strncpy(slot.name, command.name.c_str(), NAME_BUF_LEN - 1);
      slot.pulse_count = static_cast<uint16_t>(command.timings.size());
      slot.repeat_times = command.repeat_times;
      slot.gap_us = command.gap_us;
      slot.frequency_hz = command.frequency_hz;
      slot.modulation = command.modulation;
      slot.payload_crc16 = timings_crc(command.timings);
    }
    std::memcpy(raw.data() + static_cast<size_t>(i) * sizeof(StoredSlot), &slot, sizeof(StoredSlot));
  }

  const uint16_t index_crc = crc16_ccitt(raw.data(), raw.size() - 2);
  std::memcpy(raw.data() + raw.size() - 2, &index_crc, 2);
  if (!this->backend_->save(this->index_key_(), raw.data(), raw.size())) {
    return false;
  }

  StoredHeader header{};
  header.magic = STORE_MAGIC;
  header.format_version = STORE_FORMAT_VERSION;
  header.slot_count = this->max_commands_;
  header.index_bytes = static_cast<uint16_t>(index_bytes);
  header.reserved = 0;
  header.crc16 = crc16_ccitt(reinterpret_cast<const uint8_t *>(&header), sizeof(header) - 2);
  return this->backend_->save(this->header_key_(), reinterpret_cast<const uint8_t *>(&header), sizeof(header));
}

}  // namespace esphome::rf_cloner
