#pragma once

#include "esphome/core/component.h"
#include "esphome/core/defines.h"
#include "esphome/core/helpers.h"
#include "esphome/components/remote_base/remote_base.h"

#ifdef USE_TEXT_SENSOR
#include "esphome/components/text_sensor/text_sensor.h"
#endif
#ifdef USE_SENSOR
#include "esphome/components/sensor/sensor.h"
#endif

#include <functional>
#include <string>
#include <vector>

#include "capture_validator.h"
#include "command_store.h"

namespace esphome::rf_cloner {

enum class LearnState : uint8_t {
  IDLE = 0,
  ARMED,
  CAPTURED,
  FAILED,
};

const char *learn_state_to_string(LearnState state);

/// Why a learn attempt ended without storing anything.
enum class LearnFailure : uint8_t {
  NONE = 0,
  TIMEOUT,
  NO_REPEAT_AGREEMENT,
  TOO_FEW_PULSES,
  TOO_MANY_PULSES,
  PULSE_OUT_OF_RANGE,
  CANCELLED,
  NAME_REJECTED,
  STORE_ERROR,
};

const char *learn_failure_to_string(LearnFailure failure);

class RfCloner : public Component, public remote_base::RemoteReceiverListener {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  bool on_receive(remote_base::RemoteReceiveData data) override;

  void set_transmitter(remote_base::RemoteTransmitterBase *transmitter) { this->transmitter_ = transmitter; }

  void set_max_commands(uint8_t value) { this->max_commands_ = value; }
  void set_max_storage_bytes(uint32_t value) { this->max_storage_bytes_ = value; }
  void set_min_pulses(uint16_t value) { this->limits_.min_pulses = value; }
  void set_max_pulses(uint16_t value) { this->limits_.max_pulses = value; }
  void set_min_pulse_us(uint32_t value) { this->limits_.min_pulse_us = value; }
  void set_max_pulse_us(uint32_t value) { this->limits_.max_pulse_us = value; }
  void set_tolerance_percent(uint8_t value) { this->limits_.tolerance_percent = value; }
  void set_min_repeats(uint8_t value) { this->min_repeats_ = value; }
  void set_frame_gap_min_us(uint32_t value) { this->frame_gap_min_us_ = value; }
  void set_settle_ms(uint32_t value) { this->settle_ms_ = value; }
  void set_learn_timeout_ms(uint32_t value) { this->learn_timeout_ms_ = value; }
  void set_default_repeat_times(uint16_t value) { this->default_repeat_times_ = value; }
  void set_default_gap_us(uint32_t value) { this->default_gap_us_ = value; }
  void set_min_gap_us(uint32_t value) { this->min_gap_us_ = value; }
  void set_max_gap_us(uint32_t value) { this->max_gap_us_ = value; }
  void set_frequency_hz(uint32_t value) { this->frequency_hz_ = value; }
  void set_key_salt(uint32_t value) { this->key_salt_ = value; }

#ifdef USE_TEXT_SENSOR
  void set_state_text_sensor(text_sensor::TextSensor *sensor) { this->state_text_sensor_ = sensor; }
  void set_last_result_text_sensor(text_sensor::TextSensor *sensor) { this->last_result_text_sensor_ = sensor; }
#endif
#ifdef USE_SENSOR
  void set_command_count_sensor(sensor::Sensor *sensor) { this->command_count_sensor_ = sensor; }
  void set_storage_used_sensor(sensor::Sensor *sensor) { this->storage_used_sensor_ = sensor; }
#endif

  /// Arm learning under `name`; a zero timeout uses the configured default. Re-arming while
  /// already armed cancels the previous attempt.
  void learn(const std::string &name, uint32_t timeout_ms = 0);
  void cancel();
  bool send(const std::string &name, uint16_t repeat_override = 0, uint32_t gap_override = 0);
  /// Replay by immutable id, so a caller that holds one is unaffected by a rename.
  bool send_by_id(uint32_t command_id, uint16_t repeat_override = 0, uint32_t gap_override = 0);
  /// Change a command's name, keeping its id and waveform.
  bool rename(uint32_t command_id, const std::string &new_name);
  bool erase(const std::string &name);
  void clear_all();
  /// Discard the stored registry, including its identity, and write a fresh empty one. The only
  /// mutation accepted while the store is read-only, and the way to make a device carrying
  /// unreadable data usable again.
  bool factory_reset();

  /// Adopt an external bridge identity, given as 32 hex characters, and open a restore.
  bool restore_begin(const std::string &bridge_id_hex, uint32_t next_command_id);
  /// Write one exported command back under the id it had on the bridge being restored.
  bool import_command(uint32_t command_id, const std::string &name, const std::vector<int32_t> &timings,
                      uint32_t gap_us, uint16_t repeat_times, uint32_t frequency_hz, uint8_t modulation);
  bool restore_commit();

  LearnState state() const { return this->state_; }
  const std::string &last_result() const { return this->last_result_; }
  const CommandStore &store() const { return this->store_; }

  void add_on_learn_started_callback(std::function<void(std::string)> &&callback) {
    this->learn_started_callback_.add(std::move(callback));
  }
  void add_on_learn_success_callback(std::function<void(std::string)> &&callback) {
    this->learn_success_callback_.add(std::move(callback));
  }
  void add_on_learn_failed_callback(std::function<void(std::string)> &&callback) {
    this->learn_failed_callback_.add(std::move(callback));
  }
  void add_on_send_callback(std::function<void(std::string)> &&callback) {
    this->send_callback_.add(std::move(callback));
  }

 protected:
  /// Split a multi-frame window, look for agreeing neighbours, and take the gap from the space
  /// between them. Returns true when it consumed the window.
  bool consume_multi_frame_(const std::vector<int32_t> &blob);
  void commit_capture_();
  void fail_(LearnFailure failure);
  void set_state_(LearnState state);
  void publish_state_();
  void publish_store_stats_();
  uint32_t resolve_gap_us_();
  /// Shared tail of send() and send_by_id(), once a command has been resolved.
  bool transmit_(const Command &command, uint16_t repeat_override, uint32_t gap_override);

  remote_base::RemoteTransmitterBase *transmitter_{nullptr};
  CommandStore store_;
  StorageBackend *backend_{nullptr};

  // Configuration
  uint8_t max_commands_{16};
  uint32_t max_storage_bytes_{12288};
  CaptureLimits limits_{};
  uint8_t min_repeats_{3};
  uint32_t frame_gap_min_us_{3000};
  uint32_t settle_ms_{800};
  uint32_t learn_timeout_ms_{15000};
  uint16_t default_repeat_times_{20};
  uint32_t default_gap_us_{10000};
  uint32_t min_gap_us_{1000};
  uint32_t max_gap_us_{200000};
  uint32_t frequency_hz_{433920000};
  uint32_t key_salt_{0};

  // Learn state
  LearnState state_{LearnState::IDLE};
  std::string pending_name_;
  std::string last_result_;
  std::vector<int32_t> candidate_;
  uint32_t agreements_{0};
  // Span of the observation window, used by the arrival-time fallback in resolve_gap_us_().
  // Individual deltas are unusable: loop() quantisation snaps each to a multiple of a loop tick.
  uint32_t first_frame_us_{0};
  uint32_t interval_count_{0};
  /// Gaps read off the waveform when a window held several frames. Preferred over the estimate.
  std::vector<uint32_t> physical_gaps_;
  uint32_t armed_since_ms_{0};
  uint32_t deadline_ms_{0};
  uint32_t ready_since_ms_{0};
  uint32_t last_frame_us_{0};
  bool ready_{false};
  bool saw_frame_{false};
  FrameVerdict last_reject_{FrameVerdict::OK};

  CallbackManager<void(std::string)> learn_started_callback_;
  CallbackManager<void(std::string)> learn_success_callback_;
  CallbackManager<void(std::string)> learn_failed_callback_;
  CallbackManager<void(std::string)> send_callback_;

#ifdef USE_TEXT_SENSOR
  text_sensor::TextSensor *state_text_sensor_{nullptr};
  text_sensor::TextSensor *last_result_text_sensor_{nullptr};
#endif
#ifdef USE_SENSOR
  sensor::Sensor *command_count_sensor_{nullptr};
  sensor::Sensor *storage_used_sensor_{nullptr};
#endif
};

}  // namespace esphome::rf_cloner
