#include "compute_trade/optiq.hpp"

#include <algorithm>
#include <iostream>

int main() {
  namespace optiq = compute_trade::optiq;
  using optiq::Template;
  constexpr Template templates[]{
      Template::Ack, Template::Fill, Template::Reject, Template::Kill,
      Template::Logon, Template::LogonAck, Template::LogonReject,
      Template::NewOrder, Template::CancelRequest, Template::CancelReplace,
      Template::Confirm, Template::ReplayComplete, Template::ReplayGap,
  };
  optiq::Frame reused;
  for (const auto type : templates) {
    constexpr auto poison = std::byte{0xa5};
    reused.bytes.fill(poison);
    const optiq::Message message{.type = type};
    const auto fresh = optiq::encode(message);
    optiq::encode_into(message, reused);
    const auto unused = std::span(reused.bytes).subspan(reused.size);
    if (!std::ranges::equal(reused.view(), fresh.view()) ||
        !std::ranges::all_of(unused, [](std::byte byte) { return byte == poison; })) {
      std::cerr << "unexpected output or tail write for template " << static_cast<unsigned>(type) << '\n';
      return 1;
    }
  }
}
