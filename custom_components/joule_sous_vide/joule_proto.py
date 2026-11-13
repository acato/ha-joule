"""Hand-rolled protobuf encoding/decoding for the ChefSteps Joule protocol.

Only the message types used by this integration are implemented.

Wire format reference: https://protobuf.dev/programming-guides/encoding/
"""
from __future__ import annotations

import logging
import random
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire type constants
# ---------------------------------------------------------------------------
WIRETYPE_VARINT = 0
WIRETYPE_FIXED64 = 1
WIRETYPE_LENGTH_DELIMITED = 2
WIRETYPE_FIXED32 = 5

# ---------------------------------------------------------------------------
# StreamMessage oneof field numbers
# ---------------------------------------------------------------------------
FIELD_PING = 18
FIELD_PONG = 19
FIELD_START_PROGRAM_REQUEST = 50
FIELD_STOP_CIRCULATOR_REQUEST = 60
FIELD_STOP_CIRCULATOR_REPLY = 61
FIELD_BEGIN_LIVE_FEED_REQUEST = 70
FIELD_CIRCULATOR_DATA_POINT = 90
FIELD_START_KEY_EXCHANGE_REQUEST = 120
FIELD_START_KEY_EXCHANGE_REPLY = 121
FIELD_SUBMIT_KEY_REQUEST = 130
FIELD_SUBMIT_KEY_REPLY = 131
FIELD_START_PROGRAM_REPLY = 51
FIELD_IDENTIFY_CIRCULATOR_REQUEST = 152
FIELD_IDENTIFY_CIRCULATOR_REPLY = 153


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------
class JouleProtoError(Exception):
    """Raised for protobuf encoding/decoding failures."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class ProgramType(IntEnum):
    MANUAL = 0
    AUTOMATIC = 1


class ProgramStep(IntEnum):
    UNKNOWN = 0
    PRE_HEAT = 1
    WAIT_FOR_FOOD = 2
    COOK = 3
    WAIT_FOR_REMOVE_FOOD = 4
    ERROR = 5


class ErrorState(IntEnum):
    NO_ERROR = 0
    SOFT_ERROR = 1
    HARD_ERROR = 2


# ---------------------------------------------------------------------------
# Wire-format primitives
# ---------------------------------------------------------------------------
def encode_varint(value: int) -> bytes:
    """Encode an unsigned integer as a protobuf varint."""
    parts: list[int] = []
    while value > 0x7F:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    parts.append(value & 0x7F)
    return bytes(parts)


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a varint at *offset*. Returns ``(value, new_offset)``."""
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise JouleProtoError("Truncated varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            return result, offset
        shift += 7
        if shift >= 64:
            raise JouleProtoError("Varint too long")


def encode_tag(field_number: int, wire_type: int) -> bytes:
    """Encode a protobuf field tag."""
    return encode_varint((field_number << 3) | wire_type)


def encode_field_varint(field_number: int, value: int) -> bytes:
    """Encode a varint field (tag + value)."""
    return encode_tag(field_number, WIRETYPE_VARINT) + encode_varint(value)


def encode_field_float(field_number: int, value: float) -> bytes:
    """Encode a float field (tag + 4-byte LE IEEE 754)."""
    return encode_tag(field_number, WIRETYPE_FIXED32) + struct.pack("<f", value)


def encode_field_fixed32(field_number: int, value: int) -> bytes:
    """Encode a fixed32 field (tag + 4-byte LE unsigned int)."""
    return encode_tag(field_number, WIRETYPE_FIXED32) + struct.pack("<I", value)


def encode_field_bytes(field_number: int, value: bytes) -> bytes:
    """Encode a length-delimited field (tag + length + value)."""
    return (
        encode_tag(field_number, WIRETYPE_LENGTH_DELIMITED)
        + encode_varint(len(value))
        + value
    )


def decode_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Parse raw protobuf bytes into ``(field_number, wire_type, value)`` tuples.

    For varint fields ``value`` is an ``int``.
    For fixed32 fields ``value`` is 4 raw bytes.
    For fixed64 fields ``value`` is 8 raw bytes.
    For length-delimited fields ``value`` is raw ``bytes``.
    """
    fields: list[tuple[int, int, Any]] = []
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        wire_type = tag & 0x07
        field_number = tag >> 3

        if wire_type == WIRETYPE_VARINT:
            value, offset = decode_varint(data, offset)
        elif wire_type == WIRETYPE_FIXED32:
            if offset + 4 > len(data):
                raise JouleProtoError("Truncated fixed32")
            value = data[offset : offset + 4]
            offset += 4
        elif wire_type == WIRETYPE_FIXED64:
            if offset + 8 > len(data):
                raise JouleProtoError("Truncated fixed64")
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == WIRETYPE_LENGTH_DELIMITED:
            length, offset = decode_varint(data, offset)
            if offset + length > len(data):
                raise JouleProtoError("Truncated length-delimited field")
            value = data[offset : offset + length]
            offset += length
        else:
            raise JouleProtoError(f"Unsupported wire type {wire_type}")

        fields.append((field_number, wire_type, value))
    return fields


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ProgramMetadata:
    """Cook session metadata (embedded in CirculatorProgram field 6)."""

    cook_id: str = ""  # field 4, string — random UUID identifying this cook session


@dataclass
class CirculatorProgram:
    """Cook program parameters."""

    set_point: float  # field 1, float — target temp in Celsius
    cook_time: int = 0  # field 2, uint32 — seconds (0 = unlimited)
    program_type: ProgramType = ProgramType.MANUAL  # field 5, enum
    program_metadata: ProgramMetadata | None = None  # field 6, embedded message


@dataclass
class StartProgramRequest:
    """Start a cooking program."""

    circulator_program: CirculatorProgram  # field 1, embedded message
    feed_id: int = 0  # field 2, uint32 — optimistic concurrency (from CirculatorDataPoint)
    sequence_number: int = 0  # field 3, uint32 — optimistic concurrency


@dataclass
class Ping:
    """Ping handshake request (empty body)."""


@dataclass
class Pong:
    """Pong handshake response (empty body)."""


@dataclass
class StopCirculatorRequest:
    """Stop the active cooking program."""

    feed_id: int = 0  # field 2, uint32 — optimistic concurrency
    sequence_number: int = 0  # field 3, uint32 — optimistic concurrency


@dataclass
class StopCirculatorReply:
    """Device response to a StopCirculatorRequest."""

    result: int = 0  # field 1, enum Result


@dataclass
class StartKeyExchangeRequest:
    """Initiate BLE key exchange (empty body)."""


@dataclass
class StartKeyExchangeReply:
    """Device response with secret key for BLE auth."""

    secret_key: bytes = b""  # field 1, bytes
    result: int | None = None  # field 2, enum Result; must be present


@dataclass
class SubmitKeyRequest:
    """Submit the secret key received from key exchange."""

    secret_key: bytes = b""  # field 1, bytes


@dataclass
class SubmitKeyReply:
    """Device response to key submission."""

    result: int | None = None  # field 1, enum Result


@dataclass
class IdentifyCirculatorRequest:
    """Identify/handshake with the circulator (empty body)."""


@dataclass
class StartProgramReply:
    """Device response to a StartProgramRequest."""

    result: int = 0  # field 1, enum Result


@dataclass
class IdentifyCirculatorReply:
    """Device response to an IdentifyCirculatorRequest."""

    result: int = 0  # field 1, enum Result (0 = success)


@dataclass
class BeginLiveFeedRequest:
    """Request a live data feed from the device."""

    feed_id: int = 1  # field 1, uint32


@dataclass
class CirculatorDataPoint:
    """Real-time sensor readings from the device."""

    feed_id: int = 0  # field 1, uint32
    sequence_number: int = 0  # field 2, uint32
    timestamp: int = 0  # field 3, uint32
    error_state: ErrorState = ErrorState.NO_ERROR  # field 4, enum
    bath_temp: float = 0.0  # field 10, float
    program_step: ProgramStep = ProgramStep.UNKNOWN  # field 11, enum
    time_remaining: int = 0  # field 12, uint32


@dataclass
class StreamMessage:
    """Root envelope for all Joule BLE messages."""

    handle: int = 0  # field 1, fixed32 — 0 means auto-generate random handle
    end: bool = False  # field 4, bool — omitted when False (iOS app never sends it)
    sender_address: bytes = b""  # field 5, bytes
    recipient_address: bytes = b""  # field 6, bytes
    # oneof contents — at most one of these is set:
    ping: Ping | None = None  # field 18
    pong: Pong | None = None  # field 19
    start_program_request: StartProgramRequest | None = None  # field 50
    start_program_reply: StartProgramReply | None = None  # field 51
    stop_circulator_request: StopCirculatorRequest | None = None  # field 60
    stop_circulator_reply: StopCirculatorReply | None = None  # field 61
    begin_live_feed_request: BeginLiveFeedRequest | None = None  # field 70
    circulator_data_point: CirculatorDataPoint | None = None  # field 90
    start_key_exchange_request: StartKeyExchangeRequest | None = None  # field 120
    start_key_exchange_reply: StartKeyExchangeReply | None = None  # field 121
    submit_key_request: SubmitKeyRequest | None = None  # field 130
    submit_key_reply: SubmitKeyReply | None = None  # field 131
    identify_circulator_request: IdentifyCirculatorRequest | None = None  # field 152
    identify_circulator_reply: IdentifyCirculatorReply | None = None  # field 153


# ---------------------------------------------------------------------------
# Encoding functions
# ---------------------------------------------------------------------------
def encode_program_metadata(metadata: ProgramMetadata) -> bytes:
    """Encode a ProgramMetadata to protobuf bytes."""
    result = b""
    if metadata.cook_id:
        result += encode_field_bytes(4, metadata.cook_id.encode("utf-8"))
    return result


def encode_circulator_program(program: CirculatorProgram) -> bytes:
    """Encode a CirculatorProgram to protobuf bytes.

    Matches the iOS app's field ordering: setPoint(1), programType(5),
    programMetadata(6), then an extra field 7=0.  The iOS app does NOT
    include cookTime(2) for MANUAL programs; we omit it too when zero.
    """
    result = encode_field_float(1, program.set_point)
    if program.cook_time > 0:
        result += encode_field_varint(2, program.cook_time)
    # Always encode program_type — the Joule's proto2 definition declares
    # it as `required`. The iOS app always includes it (even when MANUAL=0).
    result += encode_field_varint(5, program.program_type)
    # ProgramMetadata with a cook session UUID — the iOS app always sends
    # this with a random cookId for each cook session.
    if program.program_metadata is not None:
        meta_bytes = encode_program_metadata(program.program_metadata)
        result += encode_field_bytes(6, meta_bytes)
    # Field 7 = 0: unknown purpose but the iOS app always includes it.
    # May be required by newer firmware.
    result += encode_field_varint(7, 0)
    return result


def encode_start_program_request(request: StartProgramRequest) -> bytes:
    """Encode a StartProgramRequest to protobuf bytes."""
    program_bytes = encode_circulator_program(request.circulator_program)
    result = encode_field_bytes(1, program_bytes)
    # Optimistic concurrency: feedId and sequenceNumber from the latest
    # CirculatorDataPoint.  The iOS app always sends these; the Joule may
    # reject commands that don't prove the sender has current state.
    if request.feed_id:
        result += encode_field_varint(2, request.feed_id)
    if request.sequence_number:
        result += encode_field_varint(3, request.sequence_number)
    return result


def encode_stop_circulator_request(request: StopCirculatorRequest) -> bytes:
    """Encode a StopCirculatorRequest with optimistic concurrency."""
    result = b""
    if request.feed_id:
        result += encode_field_varint(2, request.feed_id)
    if request.sequence_number:
        result += encode_field_varint(3, request.sequence_number)
    return result


def encode_identify_circulator_request(
    _request: IdentifyCirculatorRequest,
) -> bytes:
    """Encode an IdentifyCirculatorRequest (empty body)."""
    return b""


def encode_begin_live_feed_request(request: BeginLiveFeedRequest) -> bytes:
    """Encode a BeginLiveFeedRequest to protobuf bytes."""
    return encode_field_varint(1, request.feed_id)


def _random_handle() -> int:
    """Generate a random session handle for message correlation."""
    return random.randint(1, 2**31 - 1)


def encode_stream_message(message: StreamMessage) -> bytes:
    """Encode a complete StreamMessage envelope."""
    handle = message.handle if message.handle != 0 else _random_handle()
    result = encode_field_fixed32(1, handle)
    if message.end:
        result += encode_field_varint(4, 1)
    # senderAddress (field 5) and recipientAddress (field 6) are `required`
    # in the Joule's proto2 definition.  Always encode them — even when empty
    # — so the firmware's protobuf parser doesn't reject the message.
    result += encode_field_bytes(5, message.sender_address)
    result += encode_field_bytes(6, message.recipient_address)

    # Encode the oneof contents
    if message.ping is not None:
        result += encode_field_bytes(FIELD_PING, b"")
    elif message.pong is not None:
        result += encode_field_bytes(FIELD_PONG, b"")
    elif message.start_program_request is not None:
        inner = encode_start_program_request(message.start_program_request)
        result += encode_field_bytes(FIELD_START_PROGRAM_REQUEST, inner)
    elif message.stop_circulator_request is not None:
        inner = encode_stop_circulator_request(message.stop_circulator_request)
        result += encode_field_bytes(FIELD_STOP_CIRCULATOR_REQUEST, inner)
    elif message.begin_live_feed_request is not None:
        inner = encode_begin_live_feed_request(message.begin_live_feed_request)
        result += encode_field_bytes(FIELD_BEGIN_LIVE_FEED_REQUEST, inner)
    elif message.start_key_exchange_request is not None:
        result += encode_field_bytes(FIELD_START_KEY_EXCHANGE_REQUEST, b"")
    elif message.submit_key_request is not None:
        inner = encode_field_bytes(1, message.submit_key_request.secret_key)
        result += encode_field_bytes(FIELD_SUBMIT_KEY_REQUEST, inner)
    elif message.identify_circulator_request is not None:
        inner = encode_identify_circulator_request(
            message.identify_circulator_request
        )
        result += encode_field_bytes(FIELD_IDENTIFY_CIRCULATOR_REQUEST, inner)

    return result


# ---------------------------------------------------------------------------
# Decoding functions
# ---------------------------------------------------------------------------
def decode_circulator_data_point(data: bytes) -> CirculatorDataPoint:
    """Decode a CirculatorDataPoint from raw protobuf bytes."""
    point = CirculatorDataPoint()
    unknown_fields: list[str] = []
    for field_number, wire_type, value in decode_fields(data):
        if field_number == 1 and wire_type == WIRETYPE_VARINT:
            point.feed_id = value
        elif field_number == 2 and wire_type == WIRETYPE_VARINT:
            point.sequence_number = value
        elif field_number == 3 and wire_type == WIRETYPE_VARINT:
            point.timestamp = value
        elif field_number == 4 and wire_type == WIRETYPE_VARINT:
            point.error_state = ErrorState(value)
        elif field_number == 10 and wire_type == WIRETYPE_FIXED32:
            point.bath_temp = struct.unpack("<f", value)[0]
        elif field_number == 11 and wire_type == WIRETYPE_VARINT:
            point.program_step = ProgramStep(value)
        elif field_number == 12 and wire_type == WIRETYPE_VARINT:
            point.time_remaining = value
        else:
            # Log unknown fields for diagnostics — the 98-byte responses have
            # fields beyond the documented range (50-61 range observed)
            if wire_type == WIRETYPE_VARINT:
                unknown_fields.append(f"f{field_number}:v={value}")
            elif wire_type == WIRETYPE_FIXED32:
                float_val = struct.unpack("<f", value)[0]
                unknown_fields.append(
                    f"f{field_number}:f32={float_val:.4f}({value.hex()})"
                )
            elif wire_type == WIRETYPE_FIXED64:
                unknown_fields.append(f"f{field_number}:f64={value.hex()}")
            elif wire_type == WIRETYPE_LENGTH_DELIMITED:
                unknown_fields.append(f"f{field_number}:bytes={value.hex()}")
    if unknown_fields:
        _LOGGER.debug(
            "CirculatorDataPoint unknown fields: %s", ", ".join(unknown_fields),
        )
    return point


def decode_stream_message(data: bytes) -> StreamMessage:
    """Decode a StreamMessage from raw protobuf bytes."""
    message = StreamMessage()
    for field_number, wire_type, value in decode_fields(data):
        if field_number == 1 and wire_type == WIRETYPE_FIXED32:
            message.handle = struct.unpack("<I", value)[0]
        elif field_number == 4 and wire_type == WIRETYPE_VARINT:
            message.end = bool(value)
        elif field_number == 5 and wire_type == WIRETYPE_LENGTH_DELIMITED:
            message.sender_address = value
        elif field_number == 6 and wire_type == WIRETYPE_LENGTH_DELIMITED:
            message.recipient_address = value
        elif (
            field_number == FIELD_PONG
            and wire_type == WIRETYPE_LENGTH_DELIMITED
        ):
            message.pong = Pong()
        elif (
            field_number == FIELD_START_PROGRAM_REPLY
            and wire_type == WIRETYPE_LENGTH_DELIMITED
        ):
            reply = StartProgramReply()
            for fn, wt, val in decode_fields(value):
                if fn == 1 and wt == WIRETYPE_VARINT:
                    reply.result = val
            message.start_program_reply = reply
        elif (
            field_number == FIELD_STOP_CIRCULATOR_REPLY
            and wire_type == WIRETYPE_LENGTH_DELIMITED
        ):
            reply = StopCirculatorReply()
            for fn, wt, val in decode_fields(value):
                if fn == 1 and wt == WIRETYPE_VARINT:
                    reply.result = val
            message.stop_circulator_reply = reply
        elif (
            field_number == FIELD_CIRCULATOR_DATA_POINT
            and wire_type == WIRETYPE_LENGTH_DELIMITED
        ):
            message.circulator_data_point = decode_circulator_data_point(value)
        elif (
            field_number == FIELD_START_KEY_EXCHANGE_REPLY
            and wire_type == WIRETYPE_LENGTH_DELIMITED
        ):
            reply = StartKeyExchangeReply()
            for fn, wt, val in decode_fields(value):
                if fn == 1 and wt == WIRETYPE_LENGTH_DELIMITED:
                    reply.secret_key = val
                elif fn == 2 and wt == WIRETYPE_VARINT:
                    reply.result = val
            message.start_key_exchange_reply = reply
        elif (
            field_number == FIELD_SUBMIT_KEY_REPLY
            and wire_type == WIRETYPE_LENGTH_DELIMITED
        ):
            reply = SubmitKeyReply()
            for fn, wt, val in decode_fields(value):
                if fn == 1 and wt == WIRETYPE_VARINT:
                    reply.result = val
            message.submit_key_reply = reply
        elif (
            field_number == FIELD_IDENTIFY_CIRCULATOR_REPLY
            and wire_type == WIRETYPE_LENGTH_DELIMITED
        ):
            reply = IdentifyCirculatorReply()
            for fn, wt, val in decode_fields(value):
                if fn == 1 and wt == WIRETYPE_VARINT:
                    reply.result = val
            message.identify_circulator_reply = reply
        # Other oneof fields are silently ignored
    return message


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------
_DEFAULT_ADDRESS = b"\x00" * 6


def build_start_key_exchange_message(
    sender: bytes = _DEFAULT_ADDRESS,
    recipient: bytes = _DEFAULT_ADDRESS,
    handle: int = 0,
) -> bytes:
    """Build a serialized StreamMessage containing a StartKeyExchangeRequest."""
    msg = StreamMessage(
        handle=handle,
        sender_address=sender,
        recipient_address=recipient,
        start_key_exchange_request=StartKeyExchangeRequest(),
    )
    return encode_stream_message(msg)


def build_submit_key_message(
    secret_key: bytes,
    sender: bytes = _DEFAULT_ADDRESS,
    recipient: bytes = _DEFAULT_ADDRESS,
    handle: int = 0,
) -> bytes:
    """Build a serialized StreamMessage containing a SubmitKeyRequest."""
    msg = StreamMessage(
        handle=handle,
        sender_address=sender,
        recipient_address=recipient,
        submit_key_request=SubmitKeyRequest(secret_key=secret_key),
    )
    return encode_stream_message(msg)


def build_ping_message(
    sender: bytes = _DEFAULT_ADDRESS,
    recipient: bytes = _DEFAULT_ADDRESS,
    handle: int = 0,
) -> bytes:
    """Build a serialized StreamMessage containing a Ping."""
    msg = StreamMessage(
        handle=handle,
        sender_address=sender,
        recipient_address=recipient,
        ping=Ping(),
    )
    return encode_stream_message(msg)


def build_start_cook_message(
    set_point_celsius: float,
    cook_time_seconds: int,
    sender: bytes = _DEFAULT_ADDRESS,
    recipient: bytes = _DEFAULT_ADDRESS,
    handle: int = 0,
    feed_id: int = 0,
    sequence_number: int = 0,
) -> bytes:
    """Build a serialized StreamMessage containing a StartProgramRequest.

    Matches the iOS app's structure: CirculatorProgram includes a
    ProgramMetadata with a random cookId UUID, and does NOT include
    cook_time (the iOS app never sends it for MANUAL programs).
    """
    import uuid

    program = CirculatorProgram(
        set_point=set_point_celsius,
        # cook_time omitted — the iOS app never includes it for MANUAL
        # programs.  The timer is managed app-side or via the device's
        # internal state once the cook starts.
        program_type=ProgramType.MANUAL,
        program_metadata=ProgramMetadata(
            cook_id=uuid.uuid4().hex,
        ),
    )
    msg = StreamMessage(
        handle=handle,
        sender_address=sender,
        recipient_address=recipient,
        start_program_request=StartProgramRequest(
            circulator_program=program,
            feed_id=feed_id,
            sequence_number=sequence_number,
        ),
    )
    return encode_stream_message(msg)


def build_compact_start_cook_message(
    set_point_celsius: float,
    sender: bytes = b"",
    recipient: bytes = b"",
    handle: int = 0,
    feed_id: int = 0,
    sequence_number: int = 0,
) -> bytes:
    """Build a minimal StartProgramRequest (~21–30 bytes).

    At MTU=23 (20-byte ATT payload), messages >20 bytes require the BLE
    Long Write procedure (Prepare Write + Execute Write).  The full iOS-
    matching message is 68 bytes (4 Long Write chunks); auth messages are
    ~24 bytes (2 chunks) and work fine.  This compact variant omits iOS-
    only fields (ProgramMetadata, field 7) to keep the message at ~21–30
    bytes (≤2 Long Write chunks), matching the pattern that already works.

    Fields included:
    - CirculatorProgram: setPoint (field 1) + programType (field 5)
    - feedId + sequenceNumber (optimistic concurrency, when non-zero)

    Fields omitted vs. full message:
    - ProgramMetadata (field 6 of CirculatorProgram) — iOS-only
    - field 7 = 0 — unknown purpose
    - cook_time — iOS never sends it for MANUAL programs
    """
    # Build CirculatorProgram manually — just setPoint + programType
    program_bytes = encode_field_float(1, set_point_celsius)
    program_bytes += encode_field_varint(5, ProgramType.MANUAL)

    # Build StartProgramRequest
    spr_bytes = encode_field_bytes(1, program_bytes)
    if feed_id:
        spr_bytes += encode_field_varint(2, feed_id)
    if sequence_number:
        spr_bytes += encode_field_varint(3, sequence_number)

    # Build StreamMessage envelope
    h = handle if handle != 0 else _random_handle()
    result = encode_field_fixed32(1, h)
    result += encode_field_bytes(5, sender)
    result += encode_field_bytes(6, recipient)
    result += encode_field_bytes(FIELD_START_PROGRAM_REQUEST, spr_bytes)

    return result


def build_stop_cook_message(
    sender: bytes = _DEFAULT_ADDRESS,
    recipient: bytes = _DEFAULT_ADDRESS,
    handle: int = 0,
    feed_id: int = 0,
    sequence_number: int = 0,
) -> bytes:
    """Build a serialized StreamMessage containing a StopCirculatorRequest."""
    msg = StreamMessage(
        handle=handle,
        sender_address=sender,
        recipient_address=recipient,
        stop_circulator_request=StopCirculatorRequest(
            feed_id=feed_id,
            sequence_number=sequence_number,
        ),
    )
    return encode_stream_message(msg)


def build_identify_circulator_message(
    sender: bytes = _DEFAULT_ADDRESS,
    recipient: bytes = _DEFAULT_ADDRESS,
    handle: int = 0,
) -> bytes:
    """Build a serialized StreamMessage containing an IdentifyCirculatorRequest."""
    msg = StreamMessage(
        handle=handle,
        sender_address=sender,
        recipient_address=recipient,
        identify_circulator_request=IdentifyCirculatorRequest(),
    )
    return encode_stream_message(msg)


def build_live_feed_message(
    feed_id: int = 1,
    sender: bytes = _DEFAULT_ADDRESS,
    recipient: bytes = _DEFAULT_ADDRESS,
    handle: int = 0,
) -> bytes:
    """Build a serialized StreamMessage containing a BeginLiveFeedRequest."""
    msg = StreamMessage(
        handle=handle,
        sender_address=sender,
        recipient_address=recipient,
        begin_live_feed_request=BeginLiveFeedRequest(feed_id=feed_id),
    )
    return encode_stream_message(msg)


def parse_notification(data: bytes) -> CirculatorDataPoint | None:
    """Parse a BLE notification as a StreamMessage.

    Returns the ``CirculatorDataPoint`` if present, otherwise ``None``.
    Malformed data is logged and returns ``None`` (never raises).
    """
    try:
        msg = decode_stream_message(data)
        return msg.circulator_data_point
    except (JouleProtoError, Exception):  # noqa: BLE001
        _LOGGER.debug("Failed to parse notification: %s", data.hex())
        return None
