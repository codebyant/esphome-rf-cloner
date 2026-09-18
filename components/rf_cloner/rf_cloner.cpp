#include "rf_cloner.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/core/preferences.h"

#include <algorithm>
#include <cinttypes>

namespace esphome::rf_cloner {

static const char *const TAG = "rf_cloner";

// Intervals gathered before committing. The arrival-time estimate's error is bounded by
// (loop period / interval count), so a longer hold tightens it.
static const uint32_t GAP_INTERVAL_TARGET = 24;
// Below this the span is too short to average against loop quantisation.
static const uint32_t GAP_MIN_INTERVALS = 3;

const char *learn_state_to_string(LearnState state) {
  switch (state) {
    case LearnState::IDLE:
      return "idle";
    case LearnState::ARMED:
      return "armed";
    case LearnState::CAPTURED:
      return "captured";
    case LearnState::FAILED:
      return "failed";
  }
  return "unknown";
}

const char *learn_failure_to_string(LearnFailure failure) {
  switch (failure) {
    case LearnFailure::NONE:
      return "none";
    case LearnFailure::TIMEOUT:
      return "timeout";
    case LearnFailure::NO_REPEAT_AGREEMENT:
      return "no_repeat_agreement";
    case LearnFailure::TOO_FEW_PULSES:
      return "too_few_pulses";
    case LearnFailure::TOO_MANY_PULSES:
      return "too_many_pulses";
    case LearnFailure::PULSE_OUT_OF_RANGE:
      return "pulse_out_of_range";
    case LearnFailure::CANCELLED:
      return "cancelled";
    case LearnFailure::NAME_REJECTED:
      return "name_rejected";
    case LearnFailure::STORE_ERROR:
      return "store_error";
  }
  return "unknown";
}

bool store_random_bytes(uint8_t *out, size_t len) { return esphome::random_bytes(out, len); }

namespace {

/// CommandStore backed by ESPHome preferences.
///
/// Handles are cached per (key, length): ESP32Preferences::make_preference() allocates on every
/// call and ESPPreferenceObject never frees it, so one handle per save would leak on each learn.
class PreferencesBackend : public StorageBackend {
 public:
  bool save(uint32_t key, const uint8_t *data, size_t len) override {
    return this->handle_(key, len).save(data, len);
  }

  bool load(uint32_t key, uint8_t *data, size_t len) override { return this->handle_(key, len).load(data, len); }

  bool sync() override { return global_preferences->sync(); }

 protected:
  struct Cached {
    uint32_t key;
    size_t len;
    ESPPreferenceObject pref;
  };

  ESPPreferenceObject &handle_(uint32_t key, size_t len) {
    for (Cached &entry : this->cache_) {
      if (entry.key == key && entry.len == len) {
        return entry.pref;
      }
    }
    this->cache_.push_back(Cached{key, len, global_preferences->make_preference(len, key)});
    return this->cache_.back().pref;
  }

  std::vector<Cached> cache_;
};

}  // namespace

void RfCloner::setup() {
  static PreferencesBackend backend;
  this->backend_ = &backend;
  this->store_.configure(this->backend_, this->max_commands_, this->limits_.max_pulses, this->max_storage_bytes_,
                         this->key_salt_);

  const LoadReport report = this->store_.begin();
  if (!report.had_stored_data) {
    ESP_LOGI(TAG, "No stored commands yet (first boot); bridge id %s", this->store_.bridge_id_hex().c_str());
  } else if (report.fault == StoreFault::PAYLOAD_INVALID) {
    ESP_LOGE(TAG,
             "%u stored command(s) have an unreadable waveform, starting with '%s' in slot %u. The "
             "other %u loaded normally and can still be sent; every mutation is refused this "
             "session so nothing rewrites the index over them. Call rf_cloner.factory_reset to "
             "discard the registry and start clean",
             report.payload_faults, report.fault_name.c_str(), report.fault_slot, report.loaded);
  } else if (report.read_only) {
    ESP_LOGE(TAG,
             "Stored data is not readable by this firmware (%s, stored format version %u). Flash "
             "was left exactly as found and every mutation is refused this session. Call "
             "rf_cloner.factory_reset to discard it and start clean",
             store_fault_to_string(report.fault), report.stored_format_version);
  } else {
    ESP_LOGI(TAG, "Loaded %u stored command(s); bridge id %s, revision %" PRIu32, report.loaded,
             this->store_.bridge_id_hex().c_str(), this->store_.revision());
    if (report.restore_incomplete) {
      ESP_LOGW(TAG,
               "A restore was interrupted before it completed. Replay it from Home Assistant, or "
               "clear the registry to abandon it; learning is refused until then");
    }
    if (report.slot_count_changed) {
      ESP_LOGW(TAG, "max_commands changed (stored %u, configured %u)", report.stored_slot_count, this->max_commands_);
    }
    if (report.dropped_unfittable > 0) {
      ESP_LOGW(TAG,
               "%u stored command(s) no longer fit max_commands/max_pulses and were left out of "
               "the in-RAM view; their records stay on flash until the next write",
               report.dropped_unfittable);
    }
  }

  this->publish_state_();
  this->publish_store_stats_();
  this->disable_loop();
}

void RfCloner::dump_config() {
  ESP_LOGCONFIG(TAG,
                "RF Cloner:\n"
                "  Stored commands: %u / %u\n"
                "  Storage used: %" PRIu32 " of %" PRIu32 " bytes (worst case %" PRIu32 ")\n"
                "  Capture limits: %u-%u pulses, %" PRIu32 "-%" PRIu32 " us per pulse, tolerance %u%%\n"
                "  Repeat agreement: %u frames, settle %" PRIu32 " ms, learn timeout %" PRIu32 " ms\n"
                "  Replay defaults: %u repeats, %" PRIu32 " us gap\n"
                "  Capture frequency metadata: %.3f MHz",
                static_cast<unsigned>(this->store_.count()), this->max_commands_, this->store_.used_bytes(),
                this->max_storage_bytes_, this->store_.worst_case_bytes(), this->limits_.min_pulses,
                this->limits_.max_pulses, this->limits_.min_pulse_us, this->limits_.max_pulse_us,
                this->limits_.tolerance_percent, this->min_repeats_, this->settle_ms_, this->learn_timeout_ms_,
                this->default_repeat_times_, this->default_gap_us_, this->frequency_hz_ / 1e6f);

  if (this->store_.worst_case_bytes() > this->max_storage_bytes_) {
    ESP_LOGW(TAG,
             "max_commands x max_pulses could need %" PRIu32 " bytes, which exceeds max_storage_bytes (%" PRIu32
             "). Learning will be refused with 'budget_exceeded' before that point",
             this->store_.worst_case_bytes(), this->max_storage_bytes_);
  }
  if (this->transmitter_ == nullptr) {
    ESP_LOGW(TAG, "No transmitter configured; send will not work");
  }
}

void RfCloner::set_state_(LearnState state) {
  this->state_ = state;
  this->publish_state_();
}

void RfCloner::publish_state_() {
#ifdef USE_TEXT_SENSOR
  if (this->state_text_sensor_ != nullptr) {
    this->state_text_sensor_->publish_state(learn_state_to_string(this->state_));
  }
  if (this->last_result_text_sensor_ != nullptr) {
    this->last_result_text_sensor_->publish_state(this->last_result_);
  }
#endif
}

void RfCloner::publish_store_stats_() {
#ifdef USE_SENSOR
  if (this->command_count_sensor_ != nullptr) {
    this->command_count_sensor_->publish_state(static_cast<float>(this->store_.count()));
  }
  if (this->storage_used_sensor_ != nullptr) {
    this->storage_used_sensor_->publish_state(static_cast<float>(this->store_.used_bytes()));
  }
#endif
}

void RfCloner::learn(const std::string &name, uint32_t timeout_ms) {
  if (this->state_ == LearnState::ARMED) {
    ESP_LOGW(TAG, "Re-arming: cancelling the in-progress learn of '%s'", this->pending_name_.c_str());
    this->fail_(LearnFailure::CANCELLED);
  }

  const StoreResult name_check = CommandStore::validate_name(name);
  if (name_check != StoreResult::OK) {
    ESP_LOGW(TAG, "Refusing to learn: name '%s' rejected (%s)", name.c_str(), store_result_to_string(name_check));
    this->pending_name_ = name;
    this->last_result_ = std::string("name_rejected:") + store_result_to_string(name_check);
    this->set_state_(LearnState::FAILED);
    this->learn_failed_callback_.call(this->last_result_);
    this->set_state_(LearnState::IDLE);
    return;
  }

  // Refuse before the user starts pressing buttons.
  if (!this->store_.has(name) && this->store_.count() >= this->store_.max_commands()) {
    ESP_LOGW(TAG, "Refusing to learn '%s': all %u slots are in use", name.c_str(), this->store_.max_commands());
    this->pending_name_ = name;
    this->last_result_ = "store_full";
    this->set_state_(LearnState::FAILED);
    this->learn_failed_callback_.call(this->last_result_);
    this->set_state_(LearnState::IDLE);
    return;
  }

  this->pending_name_ = name;
  this->candidate_.clear();
  this->physical_gaps_.clear();
  this->agreements_ = 0;
  this->ready_ = false;
  this->saw_frame_ = false;
  this->last_reject_ = FrameVerdict::OK;
  this->armed_since_ms_ = millis();
  this->deadline_ms_ = this->armed_since_ms_ + (timeout_ms > 0 ? timeout_ms : this->learn_timeout_ms_);
  this->last_result_ = "";
  this->set_state_(LearnState::ARMED);
  this->enable_loop();

  ESP_LOGI(TAG, "Learning '%s': press and hold the remote button%s", name.c_str(),
           this->store_.has(name) ? " (this will overwrite the existing command)" : "");
  this->learn_started_callback_.call(name);
}

void RfCloner::cancel() {
  if (this->state_ != LearnState::ARMED) {
    return;
  }
  this->fail_(LearnFailure::CANCELLED);
}

bool RfCloner::on_receive(remote_base::RemoteReceiveData data) {
  if (this->state_ != LearnState::ARMED) {
    // Leave the frame for any other listener.
    return false;
  }

  std::vector<int32_t> frame = data.get_raw_data();
  strip_terminator(frame);

  // Split before validating: a multi-frame window necessarily exceeds max_pulses and would be
  // discarded as TOO_MANY_PULSES.
  if (this->consume_multi_frame_(frame)) {
    return false;
  }

  const FrameVerdict verdict = validate_frame(frame, this->limits_);
  if (verdict != FrameVerdict::OK) {
    // Noise must not abort a learn in progress. The reason is retained so a later timeout can
    // report something more specific.
    this->last_reject_ = verdict;
    ESP_LOGV(TAG, "Ignoring window of %u pulses: %s", static_cast<unsigned>(frame.size()),
             frame_verdict_to_string(verdict));
    return false;
  }

  this->saw_frame_ = true;
  const uint32_t now_us = micros();
  const uint32_t duration_us = frame_duration_us(frame);

  if (this->candidate_.empty() || !frames_agree(this->candidate_, frame, this->limits_.tolerance_percent)) {
    // First good frame, or the waveform changed.
    this->candidate_ = std::move(frame);
    this->agreements_ = 1;
    this->ready_ = false;
    this->first_frame_us_ = now_us;
    this->last_frame_us_ = now_us;
    this->interval_count_ = 0;
    return false;
  }

  this->agreements_++;

  const uint32_t delta_us = now_us - this->last_frame_us_;
  this->last_frame_us_ = now_us;
  ESP_LOGV(TAG, "  frame interval: %" PRIu32 " us (frame %" PRIu32 " us)", delta_us, duration_us);
  if (delta_us > duration_us + this->max_gap_us_) {
    // Too long to be one inter-frame gap: the button was released and pressed again. Restart the
    // window rather than averaging across the pause.
    this->first_frame_us_ = now_us;
    this->interval_count_ = 0;
  } else {
    this->interval_count_++;
  }

  if (this->agreements_ >= this->min_repeats_ && !this->ready_) {
    this->ready_ = true;
    this->ready_since_ms_ = millis();
  }
  // A longer span shrinks the estimate's error, so keep collecting while the button is held.
  if (this->ready_ && this->interval_count_ >= GAP_INTERVAL_TARGET) {
    this->commit_capture_();
  }
  return false;
}

bool RfCloner::consume_multi_frame_(const std::vector<int32_t> &blob) {
  const SplitFrames split = split_frames(blob, this->frame_gap_min_us_);
  if (split.frames.size() < 2) {
    return false;  // single frame; use the cross-callback path
  }

  // The first and last frames may be partial where the window opened or closed mid-transmission;
  // those fail to agree and are skipped.
  bool consumed = false;
  for (size_t i = 0; i + 1 < split.frames.size(); i++) {
    const std::vector<int32_t> &first = split.frames[i];
    const std::vector<int32_t> &second = split.frames[i + 1];
    if (validate_frame(first, this->limits_) != FrameVerdict::OK ||
        validate_frame(second, this->limits_) != FrameVerdict::OK) {
      continue;
    }
    if (!frames_agree(first, second, this->limits_.tolerance_percent)) {
      continue;
    }

    if (this->candidate_.empty() || !frames_agree(this->candidate_, first, this->limits_.tolerance_percent)) {
      this->candidate_ = first;
      this->agreements_ = 1;
      this->physical_gaps_.clear();
      this->ready_ = false;
    }
    this->agreements_++;
    this->physical_gaps_.push_back(split.gaps[i]);
    // Clustered readings indicate frame_gap_min is splitting correctly; scattered ones do not.
    ESP_LOGD(TAG, "  waveform gap: %" PRIu32 " us (frames %u/%u in this window)", split.gaps[i],
             static_cast<unsigned>(i + 1), static_cast<unsigned>(split.frames.size()));
    this->saw_frame_ = true;
    consumed = true;
  }

  if (!consumed) {
    return false;
  }

  ESP_LOGV(TAG, "Window held %u frames; %u physical gap(s) so far", static_cast<unsigned>(split.frames.size()),
           static_cast<unsigned>(this->physical_gaps_.size()));

  if (this->agreements_ >= this->min_repeats_ && !this->ready_) {
    this->ready_ = true;
    this->ready_since_ms_ = millis();
  }
  // A measured gap needs no averaging window.
  if (this->ready_ && this->physical_gaps_.size() >= 2) {
    this->commit_capture_();
  }
  return true;
}

void RfCloner::loop() {
  if (this->state_ != LearnState::ARMED) {
    this->disable_loop();
    return;
  }
  const uint32_t now = millis();
  if (this->ready_ && now - this->ready_since_ms_ >= this->settle_ms_) {
    this->commit_capture_();
    return;
  }
  if (!this->ready_ && static_cast<int32_t>(now - this->deadline_ms_) >= 0) {
    if (this->saw_frame_) {
      this->fail_(LearnFailure::NO_REPEAT_AGREEMENT);
    } else if (this->last_reject_ != FrameVerdict::OK) {
      switch (this->last_reject_) {
        case FrameVerdict::TOO_FEW_PULSES:
          this->fail_(LearnFailure::TOO_FEW_PULSES);
          break;
        case FrameVerdict::TOO_MANY_PULSES:
          this->fail_(LearnFailure::TOO_MANY_PULSES);
          break;
        default:
          this->fail_(LearnFailure::PULSE_OUT_OF_RANGE);
          break;
      }
    } else {
      this->fail_(LearnFailure::TIMEOUT);
    }
  }
}

uint32_t RfCloner::resolve_gap_us_() {
  // Measured gaps, from the space between two agreeing frames. The median guards against a stray
  // boundary; individual samples are already exact to a microsecond.
  if (!this->physical_gaps_.empty()) {
    std::vector<uint32_t> sorted = this->physical_gaps_;
    std::sort(sorted.begin(), sorted.end());
    const uint32_t gap = sorted[sorted.size() / 2];
    if (gap >= this->min_gap_us_ && gap <= this->max_gap_us_) {
      ESP_LOGI(TAG, "Measured inter-frame gap: %" PRIu32 " us (from the waveform, %u sample(s))", gap,
               static_cast<unsigned>(sorted.size()));
      return gap;
    }
    ESP_LOGW(TAG, "Waveform gap %" PRIu32 " us is outside %" PRIu32 "-%" PRIu32 " us", gap, this->min_gap_us_,
             this->max_gap_us_);
  }

  // Fallback for windows holding a single frame, i.e. `idle:` below the target's gap. Carries a
  // systematic bias of roughly a millisecond.
  const uint32_t frame_us = frame_duration_us(this->candidate_);
  const uint32_t span_us = this->last_frame_us_ - this->first_frame_us_;
  const uint32_t estimated = estimate_gap_us(span_us, this->interval_count_, frame_us, this->min_gap_us_,
                                             this->max_gap_us_, GAP_MIN_INTERVALS);
  if (estimated != 0) {
    ESP_LOGW(TAG,
             "Estimated inter-frame gap: %" PRIu32 " us (from arrival times over %" PRIu32
             " intervals). Raise the receiver's 'idle' above the gap to measure it directly",
             estimated, this->interval_count_);
    return estimated;
  }

  ESP_LOGW(TAG, "Could not determine the inter-frame gap; falling back to the configured default of %" PRIu32 " us",
           this->default_gap_us_);
  return this->default_gap_us_;
}

void RfCloner::commit_capture_() {
  Command command;
  command.name = this->pending_name_;
  command.timings = this->candidate_;
  command.gap_us = this->resolve_gap_us_();
  command.repeat_times = this->default_repeat_times_;
  command.frequency_hz = this->frequency_hz_;
  command.modulation = 0;  // OOK

  const StoreResult result = this->store_.put(command);
  if (result != StoreResult::OK) {
    ESP_LOGE(TAG, "Captured '%s' but could not store it: %s", command.name.c_str(), store_result_to_string(result));
    this->last_result_ = std::string("store_error:") + store_result_to_string(result);
    this->set_state_(LearnState::FAILED);
    this->learn_failed_callback_.call(this->last_result_);
    this->set_state_(LearnState::IDLE);
    return;
  }

  ESP_LOGI(TAG, "Learned '%s': %u pulses, gap %" PRIu32 " us, %u agreeing frames", command.name.c_str(),
           static_cast<unsigned>(command.timings.size()), command.gap_us, static_cast<unsigned>(this->agreements_));

  this->last_result_ = command.name;
  this->set_state_(LearnState::CAPTURED);
  this->publish_store_stats_();
  this->learn_success_callback_.call(command.name);
}

void RfCloner::fail_(LearnFailure failure) {
  ESP_LOGW(TAG, "Learning '%s' failed: %s", this->pending_name_.c_str(), learn_failure_to_string(failure));
  this->candidate_.clear();
  this->physical_gaps_.clear();
  this->agreements_ = 0;
  this->ready_ = false;
  this->last_result_ = learn_failure_to_string(failure);
  this->set_state_(LearnState::FAILED);
  this->learn_failed_callback_.call(this->last_result_);
}

bool RfCloner::send(const std::string &name, uint16_t repeat_override, uint32_t gap_override) {
  if (this->transmitter_ == nullptr) {
    ESP_LOGE(TAG, "Cannot send '%s': no transmitter configured", name.c_str());
    return false;
  }

  Command command;
  if (!this->store_.get(name, command)) {
    ESP_LOGW(TAG, "Cannot send: no command named '%s'", name.c_str());
    this->last_result_ = "not_found:" + name;
    this->publish_state_();
    return false;
  }
  return this->transmit_(command, repeat_override, gap_override);
}

bool RfCloner::send_by_id(uint32_t command_id, uint16_t repeat_override, uint32_t gap_override) {
  if (this->transmitter_ == nullptr) {
    ESP_LOGE(TAG, "Cannot send command %" PRIu32 ": no transmitter configured", command_id);
    return false;
  }

  Command command;
  if (!this->store_.get_by_id(command_id, command)) {
    ESP_LOGW(TAG, "Cannot send: no command with id %" PRIu32, command_id);
    this->last_result_ = "not_found:#" + std::to_string(command_id);
    this->publish_state_();
    return false;
  }
  return this->transmit_(command, repeat_override, gap_override);
}

bool RfCloner::rename(uint32_t command_id, const std::string &new_name) {
  const StoreResult result = this->store_.rename(command_id, new_name);
  if (result != StoreResult::OK) {
    ESP_LOGW(TAG, "Cannot rename command %" PRIu32 " to '%s': %s", command_id, new_name.c_str(),
             store_result_to_string(result));
    this->last_result_ = std::string("rename_failed:") + store_result_to_string(result);
    this->publish_state_();
    return false;
  }
  ESP_LOGI(TAG, "Renamed command %" PRIu32 " to '%s'", command_id, new_name.c_str());
  this->last_result_ = "renamed:" + new_name;
  this->publish_state_();
  return true;
}

bool RfCloner::transmit_(const Command &command, uint16_t repeat_override, uint32_t gap_override) {
  const std::string &name = command.name;
  const uint16_t repeats = repeat_override > 0 ? repeat_override : command.repeat_times;
  const uint32_t gap_us = gap_override > 0 ? gap_override : command.gap_us;

  auto call = this->transmitter_->transmit();
  auto *transmit_data = call.get_data();
  // RF: the CC1101 does its own modulation, so there is no IR carrier.
  transmit_data->set_carrier_frequency(0);
  transmit_data->set_data(command.timings);
  call.set_send_times(repeats);
  call.set_send_wait(gap_us);  // microseconds, despite the stale "ms" label in remote_base's log
  call.perform();

  ESP_LOGI(TAG, "Sent '%s' (#%" PRIu32 "): %u pulses x%u, gap %" PRIu32 " us", name.c_str(), command.command_id,
           static_cast<unsigned>(command.timings.size()), repeats, gap_us);
  this->send_callback_.call(name);
  return true;
}

bool RfCloner::erase(const std::string &name) {
  const StoreResult result = this->store_.remove(name);
  if (result != StoreResult::OK) {
    ESP_LOGW(TAG, "Cannot delete '%s': %s", name.c_str(), store_result_to_string(result));
    this->last_result_ = std::string("delete_failed:") + store_result_to_string(result);
    this->publish_state_();
    return false;
  }
  ESP_LOGI(TAG, "Deleted '%s'", name.c_str());
  this->last_result_ = "deleted:" + name;
  this->publish_state_();
  this->publish_store_stats_();
  return true;
}

void RfCloner::clear_all() {
  const StoreResult result = this->store_.clear_all();
  if (result != StoreResult::OK) {
    ESP_LOGE(TAG, "Could not clear the store: %s", store_result_to_string(result));
    this->last_result_ = std::string("clear_failed:") + store_result_to_string(result);
    this->publish_state_();
    return;
  }
  ESP_LOGI(TAG, "Cleared all stored commands");
  this->last_result_ = "cleared";
  this->publish_state_();
  this->publish_store_stats_();
}

bool RfCloner::factory_reset() {
  const StoreResult result = this->store_.factory_reset();
  if (result != StoreResult::OK) {
    ESP_LOGE(TAG, "Factory reset failed: %s", store_result_to_string(result));
    this->last_result_ = std::string("factory_reset_failed:") + store_result_to_string(result);
    this->publish_state_();
    return false;
  }
  ESP_LOGW(TAG, "Factory reset: registry discarded, new bridge id %s", this->store_.bridge_id_hex().c_str());
  this->last_result_ = "factory_reset";
  this->publish_state_();
  this->publish_store_stats_();
  return true;
}

}  // namespace esphome::rf_cloner
