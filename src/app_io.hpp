#pragma once

#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>

namespace compute_trade::app_io {

class Socket {
  int fd_{-1};
public:
  Socket() = default;
  explicit Socket(int fd) : fd_(fd) {}
  Socket(Socket &&other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
  Socket &operator=(Socket &&other) noexcept {
    if (this != &other) {
      reset();
      fd_ = std::exchange(other.fd_, -1);
    }
    return *this;
  }
  ~Socket() { reset(); }
  int get() const { return fd_; }
  explicit operator bool() const { return fd_ >= 0; }
  void reset() {
    if (*this)
      ::close(std::exchange(fd_, -1));
  }
};

inline bool retryable() { return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR; }

[[noreturn]] inline void socket_error(const char *operation) {
  throw std::system_error(errno, std::generic_category(), operation);
}

inline Socket listener(const std::string &ip, std::uint16_t requested_port,
                       std::uint16_t &port) {
  Socket fd(::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
  if (!fd)
    socket_error("socket");
  const int one = 1;
  if (::setsockopt(fd.get(), SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) != 0)
    socket_error("SO_REUSEADDR");
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(requested_port);
  if (::inet_pton(AF_INET, ip.c_str(), &address.sin_addr) != 1)
    throw std::invalid_argument("bind address must be IPv4");
  if (::bind(fd.get(), reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0)
    socket_error("bind");
  if (::listen(fd.get(), 32) != 0)
    socket_error("listen");
  socklen_t length = sizeof(address);
  if (::getsockname(fd.get(), reinterpret_cast<sockaddr *>(&address), &length) != 0)
    socket_error("getsockname");
  port = ntohs(address.sin_port);
  return fd;
}

inline Socket accept(Socket &listener, bool tcp = true) {
  Socket fd(::accept4(listener.get(), nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC));
  if (!fd && !retryable() && errno != ECONNABORTED)
    socket_error("accept");
  const int one = 1;
  if (fd && tcp && ::setsockopt(fd.get(), IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one)) != 0)
    socket_error("TCP_NODELAY");
  return fd;
}

class UnixListener {
  std::string path_;
  Socket fd_;
public:
  explicit UnixListener(const std::string &path) {
    if (path.empty())
      return;
    sockaddr_un address{};
    if (path.size() >= sizeof(address.sun_path))
      throw std::invalid_argument("control socket path is too long");
    Socket fd(::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
    if (!fd)
      socket_error("control socket");
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, path.c_str(), path.size() + 1);
    if (::bind(fd.get(), reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0)
      socket_error("control bind");
    path_ = path;
    if (::chmod(path_.c_str(), S_IRUSR | S_IWUSR) != 0 || ::listen(fd.get(), 16) != 0) {
      const auto error = errno;
      ::unlink(path_.c_str());
      throw std::system_error(error, std::generic_category(), "control listen");
    }
    fd_ = std::move(fd);
  }

  ~UnixListener() {
    if (fd_)
      ::unlink(path_.c_str());
  }

  Socket &socket() { return fd_; }
};

} // namespace compute_trade::app_io
