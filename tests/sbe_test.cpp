#include "compute_trade/sbe.hpp"

#include <algorithm>
#include <array>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace {

using namespace compute_trade;

void check(bool condition, std::string_view message) {
    if (!condition) {
        throw std::runtime_error(std::string(message));
    }
}

template <class Function>
void rejects(Function&& function, std::string_view message) {
    try {
        function();
    } catch (const std::invalid_argument&) {
        return;
    }
    throw std::runtime_error(std::string(message));
}

template <std::size_t N>
void golden(const std::array<std::byte, N>& actual, const std::array<unsigned char, N>& expected) {
    for (std::size_t i = 0; i < N; ++i) {
        check(std::to_integer<unsigned char>(actual[i]) == expected[i], "golden wire bytes differ");
    }
}

void new_order_golden() {
    const NewOrder order{0x0102030405060708ULL, 0x090A0B0CU, Side::Buy,
                         0x1112131415161718LL, 0x2122232425262728ULL};
    constexpr std::array<unsigned char, 43> expected{
        0x00, 0x00, 0x00, 0x2B, 0xEB, 0x50,
        0x1D, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00,
        0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01,
        0x0C, 0x0B, 0x0A, 0x09, 0x01,
        0x18, 0x17, 0x16, 0x15, 0x14, 0x13, 0x12, 0x11,
        0x28, 0x27, 0x26, 0x25, 0x24, 0x23, 0x22, 0x21
    };
    const auto encoded = sbe::encode(order);
    golden(encoded, expected);
    const auto decoded = sbe::decode_new_order(encoded);
    check(decoded.client_order_id == order.client_order_id &&
          decoded.instrument_id == order.instrument_id && decoded.side == order.side &&
          decoded.price == order.price && decoded.quantity == order.quantity,
          "new order roundtrip");
    check(sbe::frame_length(std::span(encoded).first(6)) == encoded.size(), "SOFH-only framing");
}

void acknowledgement_golden() {
    const Acknowledgement acknowledgement{1, 2, 3, Status::Accepted, RejectReason::None, 4, 5};
    constexpr std::array<unsigned char, 52> expected{
        0x00, 0x00, 0x00, 0x34, 0xEB, 0x50,
        0x26, 0x00, 0x02, 0x00, 0x01, 0x00, 0x00, 0x00,
        1, 0, 0, 0, 0, 0, 0, 0,
        2, 0, 0, 0, 0, 0, 0, 0,
        3, 0, 0, 0, 0, 0,
        4, 0, 0, 0, 0, 0, 0, 0,
        5, 0, 0, 0, 0, 0, 0, 0
    };
    const auto encoded = sbe::encode(acknowledgement);
    golden(encoded, expected);
    const auto decoded = sbe::decode_acknowledgement(encoded);
    check(decoded.client_order_id == 1 && decoded.order_id == 2 &&
          decoded.instrument_id == 3 && decoded.status == Status::Accepted &&
          decoded.reason == RejectReason::None && decoded.executed_quantity == 4 &&
          decoded.remaining_quantity == 5, "acknowledgement roundtrip");
}

void boundaries() {
    // The codec preserves primitive bits; business validity is checked by the OEG.
    for (const auto price : {std::numeric_limits<std::int64_t>::min(), std::int64_t{-1},
                             std::int64_t{0}, std::numeric_limits<std::int64_t>::max()}) {
        const NewOrder order{std::numeric_limits<std::uint64_t>::max(),
                             std::numeric_limits<std::uint32_t>::max(),
                             Side::Sell, price, std::numeric_limits<std::uint64_t>::max()};
        const auto decoded = sbe::decode_new_order(sbe::encode(order));
        check(decoded.price == price && decoded.client_order_id == order.client_order_id &&
              decoded.quantity == order.quantity && decoded.instrument_id == order.instrument_id &&
              decoded.side == Side::Sell, "primitive boundary roundtrip");
    }
    for (std::uint8_t reason = 0; reason <= 4; ++reason) {
        const Acknowledgement original{9, 0, 1, Status::Rejected,
                                       static_cast<RejectReason>(reason), 0, 0};
        const auto decoded = sbe::decode_acknowledgement(sbe::encode(original));
        check(decoded.reason == original.reason && decoded.status == original.status,
              "rejection enumeration roundtrip");
    }
    auto invalid_side = sbe::encode(NewOrder{1, 1, Side::Buy, 10, 1});
    invalid_side[26] = std::byte{7};
    check(static_cast<std::uint8_t>(sbe::decode_new_order(invalid_side).side) == 7,
          "invalid side preserved for business rejection");
}

void malformed() {
    const auto valid = sbe::encode(NewOrder{1, 1, Side::Buy, 100, 10});
    for (std::size_t length = 0; length < valid.size(); ++length) {
        rejects([&] { (void)sbe::decode_new_order(std::span(valid).first(length)); },
                "accepted truncated order");
    }
    for (const std::size_t offset : {4U, 5U, 6U, 8U, 10U, 12U}) {
        auto bytes = valid;
        bytes[offset] ^= std::byte{0x01};
        rejects([&] { (void)sbe::decode_new_order(bytes); }, "accepted bad encoding/header");
    }
    for (const auto length : {0U, 5U, 13U, 42U, 44U, 257U, 0xFFFFFFFFU}) {
        auto bytes = valid;
        for (std::size_t i = 0; i < 4; ++i) {
            bytes[i] = static_cast<std::byte>((length >> (8 * (3 - i))) & 0xFF);
        }
        rejects([&] { (void)sbe::decode_new_order(bytes); }, "accepted bad frame length");
    }
    std::vector<std::byte> extra(valid.begin(), valid.end());
    extra.push_back(std::byte{0});
    rejects([&] { (void)sbe::decode_new_order(extra); }, "accepted trailing data");
    rejects([&] { (void)sbe::decode_acknowledgement(valid); }, "accepted wrong message type");

    const auto valid_ack = sbe::encode(Acknowledgement{});
    for (std::size_t length = 0; length < valid_ack.size(); ++length) {
        rejects([&] { (void)sbe::decode_acknowledgement(std::span(valid_ack).first(length)); },
                "accepted truncated acknowledgement");
    }
    for (const std::size_t offset : {34U, 35U}) {
        auto bytes = valid_ack;
        bytes[offset] = std::byte{0xFF};
        rejects([&] { (void)sbe::decode_acknowledgement(bytes); }, "accepted invalid ack enum");
    }
}

} // namespace

int main() {
    try {
        new_order_golden();
        acknowledgement_golden();
        boundaries();
        malformed();
        std::cout << "SBE codec tests passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
