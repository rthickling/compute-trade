/* Generated SBE (Simple Binary Encoding) message codec */
#ifndef _COMPUTE_TRADE_WIRE_REJECTREASON_CXX_H_
#define _COMPUTE_TRADE_WIRE_REJECTREASON_CXX_H_

#if !defined(__STDC_LIMIT_MACROS)
#  define __STDC_LIMIT_MACROS 1
#endif

#include <cstdint>
#include <iomanip>
#include <limits>
#include <ostream>
#include <stdexcept>
#include <sstream>
#include <string>

#define SBE_NULLVALUE_INT8 (std::numeric_limits<std::int8_t>::min)()
#define SBE_NULLVALUE_INT16 (std::numeric_limits<std::int16_t>::min)()
#define SBE_NULLVALUE_INT32 (std::numeric_limits<std::int32_t>::min)()
#define SBE_NULLVALUE_INT64 (std::numeric_limits<std::int64_t>::min)()
#define SBE_NULLVALUE_UINT8 (std::numeric_limits<std::uint8_t>::max)()
#define SBE_NULLVALUE_UINT16 (std::numeric_limits<std::uint16_t>::max)()
#define SBE_NULLVALUE_UINT32 (std::numeric_limits<std::uint32_t>::max)()
#define SBE_NULLVALUE_UINT64 (std::numeric_limits<std::uint64_t>::max)()

namespace compute_trade {
namespace wire {

class RejectReason
{
public:
    enum Value
    {
        None = static_cast<std::uint8_t>(0),
        MalformedMessage = static_cast<std::uint8_t>(1),
        InvalidOrder = static_cast<std::uint8_t>(2),
        UnknownInstrument = static_cast<std::uint8_t>(3),
        DuplicateClientOrderId = static_cast<std::uint8_t>(4),
        NULL_VALUE = static_cast<std::uint8_t>(255)
    };

    static RejectReason::Value get(const std::uint8_t value)
    {
        switch (value)
        {
            case static_cast<std::uint8_t>(0): return None;
            case static_cast<std::uint8_t>(1): return MalformedMessage;
            case static_cast<std::uint8_t>(2): return InvalidOrder;
            case static_cast<std::uint8_t>(3): return UnknownInstrument;
            case static_cast<std::uint8_t>(4): return DuplicateClientOrderId;
            case static_cast<std::uint8_t>(255): return NULL_VALUE;
        }

        throw std::runtime_error("unknown value for enum RejectReason [E103]");
    }

    static const char *c_str(const RejectReason::Value value)
    {
        switch (value)
        {
            case None: return "None";
            case MalformedMessage: return "MalformedMessage";
            case InvalidOrder: return "InvalidOrder";
            case UnknownInstrument: return "UnknownInstrument";
            case DuplicateClientOrderId: return "DuplicateClientOrderId";
            case NULL_VALUE: return "NULL_VALUE";
        }

        throw std::runtime_error("unknown value for enum RejectReason [E103]:");
    }

    template<typename CharT, typename Traits>
    friend std::basic_ostream<CharT, Traits> & operator << (
        std::basic_ostream<CharT, Traits> &os, RejectReason::Value m)
    {
        return os << RejectReason::c_str(m);
    }
};

}
}

#endif
