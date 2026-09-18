#pragma once

#include "esphome/core/automation.h"
#include "rf_cloner.h"

#include <string>
#include <utility>

namespace esphome::rf_cloner {

template<typename... Ts> class LearnAction : public Action<Ts...>, public Parented<RfCloner> {
 public:
  TEMPLATABLE_VALUE(std::string, name)
  TEMPLATABLE_VALUE(uint32_t, timeout)

  void play(const Ts &...x) override { this->parent_->learn(this->name_.value(x...), this->timeout_.value_or(x..., 0)); }
};

template<typename... Ts> class CancelAction : public Action<Ts...>, public Parented<RfCloner> {
 public:
  void play(const Ts &...x) override { this->parent_->cancel(); }
};

template<typename... Ts> class SendAction : public Action<Ts...>, public Parented<RfCloner> {
 public:
  TEMPLATABLE_VALUE(std::string, name)
  TEMPLATABLE_VALUE(uint16_t, repeat_times)
  TEMPLATABLE_VALUE(uint32_t, gap)

  void play(const Ts &...x) override {
    this->parent_->send(this->name_.value(x...), this->repeat_times_.value_or(x..., 0), this->gap_.value_or(x..., 0));
  }
};

template<typename... Ts> class SendByIdAction : public Action<Ts...>, public Parented<RfCloner> {
 public:
  TEMPLATABLE_VALUE(uint32_t, command_id)
  TEMPLATABLE_VALUE(uint16_t, repeat_times)
  TEMPLATABLE_VALUE(uint32_t, gap)

  void play(const Ts &...x) override {
    this->parent_->send_by_id(this->command_id_.value(x...), this->repeat_times_.value_or(x..., 0),
                              this->gap_.value_or(x..., 0));
  }
};

template<typename... Ts> class RenameAction : public Action<Ts...>, public Parented<RfCloner> {
 public:
  TEMPLATABLE_VALUE(uint32_t, command_id)
  TEMPLATABLE_VALUE(std::string, name)

  void play(const Ts &...x) override { this->parent_->rename(this->command_id_.value(x...), this->name_.value(x...)); }
};

template<typename... Ts> class DeleteAction : public Action<Ts...>, public Parented<RfCloner> {
 public:
  TEMPLATABLE_VALUE(std::string, name)

  void play(const Ts &...x) override { this->parent_->erase(this->name_.value(x...)); }
};

template<typename... Ts> class DeleteByIdAction : public Action<Ts...>, public Parented<RfCloner> {
 public:
  TEMPLATABLE_VALUE(uint32_t, command_id)

  void play(const Ts &...x) override { this->parent_->erase_by_id(this->command_id_.value(x...)); }
};

template<typename... Ts> class ClearAllAction : public Action<Ts...>, public Parented<RfCloner> {
 public:
  void play(const Ts &...x) override { this->parent_->clear_all(); }
};

template<typename... Ts> class FactoryResetAction : public Action<Ts...>, public Parented<RfCloner> {
 public:
  void play(const Ts &...x) override { this->parent_->factory_reset(); }
};

class LearnStartedTrigger : public Trigger<std::string> {
 public:
  explicit LearnStartedTrigger(RfCloner *parent) {
    parent->add_on_learn_started_callback([this](std::string name) { this->trigger(std::move(name)); });
  }
};

class LearnSuccessTrigger : public Trigger<std::string> {
 public:
  explicit LearnSuccessTrigger(RfCloner *parent) {
    parent->add_on_learn_success_callback([this](std::string name) { this->trigger(std::move(name)); });
  }
};

class LearnFailedTrigger : public Trigger<std::string> {
 public:
  explicit LearnFailedTrigger(RfCloner *parent) {
    parent->add_on_learn_failed_callback([this](std::string reason) { this->trigger(std::move(reason)); });
  }
};

class SendTrigger : public Trigger<std::string> {
 public:
  explicit SendTrigger(RfCloner *parent) {
    parent->add_on_send_callback([this](std::string name) { this->trigger(std::move(name)); });
  }
};

}  // namespace esphome::rf_cloner
