#pragma once

#include <json-c/json.h>
#include <concepts>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

namespace compute_trade {

class Json {
  json_object *value_{};

  explicit Json(json_object *value) : value_(value) {}

  void expect(json_type type) const {
    if (!json_object_is_type(value_, type))
      throw std::invalid_argument("unexpected JSON value type");
  }

public:
  Json() = default;
  Json(bool value) : value_(json_object_new_boolean(value)) {}
  template <std::signed_integral T>
  Json(T value) : value_(json_object_new_int64(value)) {}
  template <std::unsigned_integral T>
  Json(T value) : value_(json_object_new_uint64(value)) {}
  Json(std::string_view value)
      : value_(json_object_new_string_len(value.data(), static_cast<int>(value.size()))) {}
  Json(const std::string &value) : Json(std::string_view(value)) {}
  Json(const char *value) : Json(std::string_view(value)) {}
  Json(const Json &other) : value_(json_object_get(other.value_)) {}
  Json(Json &&other) noexcept : value_(std::exchange(other.value_, nullptr)) {}
  ~Json() { json_object_put(value_); }

  Json &operator=(Json other) noexcept {
    std::swap(value_, other.value_);
    return *this;
  }

  static Json object() { return Json(json_object_new_object()); }
  static Json array() { return Json(json_object_new_array()); }

  static Json parse(std::string_view input) {
    if (input.size() >= static_cast<std::size_t>(std::numeric_limits<int>::max()))
      throw std::invalid_argument("JSON is too large");
    auto *parser = json_tokener_new();
    if (!parser)
      throw std::bad_alloc();
    json_tokener_set_flags(parser, JSON_TOKENER_STRICT | JSON_TOKENER_VALIDATE_UTF8);
    const std::string text(input);
    Json parsed(json_tokener_parse_ex(parser, text.c_str(), static_cast<int>(text.size() + 1)));
    const auto error = json_tokener_get_error(parser);
    json_tokener_free(parser);
    if (error != json_tokener_success)
      throw std::invalid_argument(json_tokener_error_desc(error));
    return parsed;
  }

  bool is_null() const { return !value_; }

  bool has(const char *name) const {
    expect(json_type_object);
    return json_object_object_get_ex(value_, name, nullptr);
  }

  Json get(const char *name) const {
    expect(json_type_object);
    return Json(json_object_get(json_object_object_get(value_, name)));
  }

  Json at(std::size_t index) const {
    expect(json_type_array);
    if (index >= size())
      throw std::out_of_range("JSON array index");
    return Json(json_object_get(json_object_array_get_idx(value_, index)));
  }

  std::size_t size() const {
    expect(json_type_array);
    return json_object_array_length(value_);
  }

  std::uint64_t number() const {
    expect(json_type_int);
    if (json_object_get_int64(value_) < 0)
      throw std::invalid_argument("expected an unsigned JSON integer");
    return json_object_get_uint64(value_);
  }

  bool boolean() const {
    expect(json_type_boolean);
    return json_object_get_boolean(value_);
  }

  std::string_view string() const {
    expect(json_type_string);
    return {json_object_get_string(value_), static_cast<std::size_t>(json_object_get_string_len(value_))};
  }

  Json &set(const char *name, Json member) {
    expect(json_type_object);
    if (json_object_object_add(value_, name, member.value_) != 0)
      throw std::bad_alloc();
    member.value_ = nullptr;
    return *this;
  }

  void push(Json member) {
    expect(json_type_array);
    if (json_object_array_add(value_, member.value_) != 0)
      throw std::bad_alloc();
    member.value_ = nullptr;
  }

  bool includes(const Json &fields) const {
    fields.expect(json_type_object);
    json_object_object_foreach(fields.value_, name, member) {
      json_object *actual{};
      if (!json_object_object_get_ex(value_, name, &actual) ||
          !json_object_equal(actual, member))
        return false;
    }
    return true;
  }

  std::string dump() const {
    return value_ ? json_object_to_json_string_ext(value_, JSON_C_TO_STRING_PLAIN) : "null";
  }
};

} // namespace compute_trade
