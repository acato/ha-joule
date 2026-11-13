"""Pairing protocol regression tests; no radio or heater operations."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.joule_sous_vide.coordinator import JouleCoordinator
from custom_components.joule_sous_vide.joule_proto import (
    FIELD_START_KEY_EXCHANGE_REPLY, FIELD_SUBMIT_KEY_REPLY,
    decode_stream_message, decode_fields, encode_field_bytes, encode_field_varint,
)


def key_reply(key=b'test-key', result=0):
    return encode_field_bytes(
        FIELD_START_KEY_EXCHANGE_REPLY,
        encode_field_bytes(1, key) + encode_field_varint(2, result),
    )


def submit_reply(result=0):
    return encode_field_bytes(FIELD_SUBMIT_KEY_REPLY, encode_field_varint(1, result))


@pytest.fixture
def pairing(hass, mock_config_entry, mock_ble_api):
    mock_config_entry.add_to_hass(hass)
    return JouleCoordinator(hass, mock_config_entry)


async def test_delayed_button_sends_one_request_despite_unrelated_notifications(
    pairing, mock_ble_api, caplog,
):
    pairing.KEY_EXCHANGE_TIMEOUT = 0.5
    pairing._submit_key = AsyncMock(return_value=True)
    task = asyncio.create_task(pairing._key_exchange())
    await asyncio.sleep(0.02)
    assert not task.done()
    assert mock_ble_api.write_message.await_count == 1
    payload = mock_ble_api.write_message.await_args.args[0]
    assert len(payload) <= 20
    request = decode_stream_message(payload)
    assert (120, 2, b'') in list(decode_fields(payload))
    assert request.sender_address == request.recipient_address == b''
    # An empty characteristic read or unrecognized notification must not
    # finish the physical-button confirmation window.
    await pairing._read_and_process()
    pairing._try_decode_message(b'\x08\x01', 'unrelated')
    await asyncio.sleep(0.02)
    assert not task.done()
    assert mock_ble_api.write_message.await_count == 1
    pairing._try_decode_message(key_reply(), 'button')
    assert await task is True
    mock_ble_api.write_message_no_response.assert_not_awaited()
    pairing._submit_key.assert_awaited_once_with(b'test-key')
    assert pairing._entry.options['ble_auth_key'] == b'test-key'.hex()
    assert b'test-key'.hex() not in caplog.text
    assert key_reply().hex() not in caplog.text


@pytest.mark.parametrize('key,result', [(b'', 0), (b'test-key', 7)])
async def test_invalid_key_reply_is_never_persisted(pairing, mock_ble_api, key, result):
    pairing.KEY_EXCHANGE_TIMEOUT = 0.5
    pairing._submit_key = AsyncMock()
    task = asyncio.create_task(pairing._key_exchange())
    await asyncio.sleep(0.01)
    pairing._try_decode_message(key_reply(key, result), 'button')
    assert await task is False
    assert pairing._auth_key is None
    assert 'ble_auth_key' not in pairing._entry.options
    pairing._submit_key.assert_not_awaited()


@pytest.mark.parametrize('result,expected', [(0, True), (7, False)])
async def test_submit_result_controls_authentication(pairing, mock_ble_api, result, expected):
    async def write(payload):
        assert any(fn == 130 for fn, _, _ in decode_fields(payload))
        pairing._try_decode_message(submit_reply(result), 'submit')
    mock_ble_api.write_message.side_effect = write
    assert await pairing._submit_key(b'test-key') is expected
    assert pairing._authenticated is expected


async def test_no_button_times_out_without_sending_more_variants(pairing, mock_ble_api):
    pairing.KEY_EXCHANGE_TIMEOUT = 0.05
    start = asyncio.get_running_loop().time()
    assert await pairing._key_exchange() is False
    assert asyncio.get_running_loop().time() - start >= 0.05
    assert mock_ble_api.write_message.await_count == 1
    mock_ble_api.write_message_no_response.assert_not_awaited()


@pytest.mark.parametrize('result', [0, 13])
async def test_stale_key_buffer_does_not_confirm_new_pairing(pairing, mock_ble_api, result):
    pairing.KEY_EXCHANGE_TIMEOUT = 0.06
    mock_ble_api.is_connected_via_proxy = True
    mock_ble_api.read_characteristic.return_value = key_reply(b'old-key', result)
    pairing._submit_key = AsyncMock()
    start = asyncio.get_running_loop().time()
    assert await pairing._key_exchange() is False
    assert asyncio.get_running_loop().time() - start >= 0.06
    assert pairing._auth_key is None
    assert 'ble_auth_key' not in pairing._entry.options
    pairing._submit_key.assert_not_awaited()
    assert mock_ble_api.write_message.await_count == 1
    assert pairing._pending_auth_reply is None
    assert mock_ble_api.read_characteristic.await_count > 1


async def test_missing_result_is_rejected(pairing, mock_ble_api):
    async def submit(payload):
        pairing._try_decode_message(encode_field_bytes(FIELD_SUBMIT_KEY_REPLY, b''), 'submit')
    mock_ble_api.write_message.side_effect = submit
    assert await pairing._submit_key(b'test-key') is False
    assert mock_ble_api.write_message.await_count == 1
    assert not pairing._authenticated


async def test_no_submit_reply_does_not_send_long_address_fallback(pairing, mock_ble_api):
    assert await pairing._submit_key(b'test-key') is False
    assert mock_ble_api.write_message.await_count == 1


async def test_missing_key_result_is_rejected(pairing, mock_ble_api):
    async def send(payload):
        pairing._try_decode_message(
            encode_field_bytes(FIELD_START_KEY_EXCHANGE_REPLY, encode_field_bytes(1, b'test-key')),
            'key',
        )
    mock_ble_api.write_message.side_effect = send
    assert await pairing._key_exchange() is False
    assert pairing._auth_key is None
    assert 'ble_auth_key' not in pairing._entry.options


async def test_publish_before_telemetry_does_not_invent_temperature(pairing):
    pairing._publish_state()
    assert pairing.data['current_temperature'] is None
    await pairing.async_shutdown()


async def test_changed_polled_key_reply_is_persisted_and_reloaded(pairing, mock_ble_api):
    pairing.KEY_EXCHANGE_TIMEOUT = 0.1
    mock_ble_api.is_connected_via_proxy = True
    old = key_reply(b"old-key", 13)
    fresh = key_reply(b"fresh-key", 0)
    reads = 0

    async def read(_uuid):
        nonlocal reads
        reads += 1
        return old if reads == 1 else fresh

    mock_ble_api.read_characteristic.side_effect = read
    pairing._submit_key = AsyncMock(return_value=True)
    assert await pairing._key_exchange() is True
    assert pairing._entry.options["ble_auth_key"] == b"fresh-key".hex()
    reloaded = JouleCoordinator(pairing.hass, pairing._entry)
    assert reloaded._auth_key == b"fresh-key"
    await reloaded.async_shutdown()
