#!/usr/bin/env python3
"""Capture sanitized, read-only Rivian API responses for compatibility research."""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections.abc import Awaitable, Iterable, Mapping
import contextlib
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import sys
from typing import Any
from uuid import uuid4

import aiohttp
from rivian import Rivian
from rivian.const import (
    LIVE_SESSION_PROPERTIES,
    VEHICLE_STATES_SUBSCRIPTION_PROPERTIES,
)
from rivian.exceptions import (
    RivianApiRateLimitError,
    RivianTemporarilyLockedError,
    RivianUnauthenticated,
)
from rivian.rivian import (
    APOLLO_CLIENT_NAME,
    BASE_HEADERS,
    GRAPHQL_CHARGING,
    GRAPHQL_GATEWAY,
    GRAPHQL_WEBSOCKET,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from custom_components.rivian.const import (  # noqa: E402
    CHARGING_API_FIELDS,
    VEHICLE_STATE_API_FIELDS,
)

CONFIG_ENTRIES_PATH = Path("config/.storage/core.config_entries")
DEFAULT_OUTPUT_PATH = Path("config/rivian-research")
LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
AUTH_QUERY_VALUE_PATTERN = re.compile(
    r"(?i)([?&](?:"
    r"x-amz-(?:credential|security-token|signature)|"
    r"access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"authorization|credential|signature"
    r")=)[^&#\s]+"
)
READ_ONLY_QUERY_SETS = {
    "account",
    "vehicle-state",
    "charging",
    "history",
    "ota-images",
}

SECRET_ENTRY_KEYS = {
    "access_token",
    "password",
    "refresh_token",
    "user_session_token",
}
AUTH_SECRET_KEYS = {
    "access_token",
    "accessToken",
    "app_session_token",
    "appSessionToken",
    "authorization",
    "cookie",
    "csrf_token",
    "csrfToken",
    "headers",
    "otp",
    "password",
    "refresh_token",
    "refreshToken",
    "requestHeaders",
    "setCookie",
    "user_session_token",
    "userSessionToken",
}
AUTH_SECRET_KEYS_LOWER = {key.lower() for key in AUTH_SECRET_KEYS}
REDACTED = "[redacted]"

PARALLAX_RVMS = (
    "body.closures.states",
    "body.locks.states",
    "body.trailer.state",
    "body.windows.states",
    "charging.schedule.time_window",
    "charging.session.notification",
    "charging.session.remote_command",
    "charging.session.soc_slider",
    "charging.session.status",
    "charging.session.time_estimation",
    "charging.session.trip_target",
    "comfort.cabin.cabin_preconditioning_status",
    "comfort.cabin.cabin_temperatures",
    "comfort.cabin.cabin_ventilation_setting",
    "comfort.cabin.climate_hold_setting",
    "comfort.cabin.climate_hold_status",
    "comfort.cabin.defrost_defog_status",
    "comfort.cabin.hvac_settings_status",
    "comfort.cabin.pet_mode_status",
    "comfort.cabin.seat_conditioning_status",
    "comfort.user_modes.state",
    "dynamics.tires.state",
    "dynamics.vehicle.drive_mode",
    "dynamics.vehicle.gear",
    "dynamics.vehicle.gnss",
    "dynamics.vehicle.location",
    "dynamics.vehicle.odometer",
    "dynamics.vehicle.range",
    "energy.high_voltage.battery_characteristics",
    "energy.high_voltage.battery_state",
    "energy.low_voltage.battery_state",
    "energy_edge_compute.graphs.charge_session_breakdown",
    "energy_edge_compute.graphs.charging_graph_global",
    "energy_edge_compute.graphs.cold_weather_soc",
    "energy_edge_compute.graphs.parked_energy_distributions",
    "navigation.navigation_service.trip_info",
    "navigation.navigation_service.trip_progress",
    "ota.deployment.state",
    "ota.ota_state.vehicle_ota_state",
    "ota.user_schedule.ota_config",
    "vehicle.network.state",
    "vehicle.power.state",
    "vehicle.wheels.vehicle_wheels",
)

CORRELATION_RVMS = (
    "body.closures.states",
    "body.locks.states",
    "body.windows.states",
    "charging.session.status",
    "dynamics.vehicle.drive_mode",
    "dynamics.vehicle.gear",
    "vehicle.power.state",
)
CORRELATION_CHECKLIST = (
    "each-door-open-and-closed",
    "frunk-open-and-closed",
    "liftgate-open-and-closed",
    "each-window-open-and-closed",
    "charge-port-open-and-closed-if-exposed",
    "vehicle-locked-and-unlocked",
    "gear-park-reverse-neutral-drive-and-return-to-park",
    "each-available-drive-mode-and-return-to-baseline",
)
PARALLAX_SUBSCRIPTION_QUERY = """
    subscription ParallaxMessages($vehicleId: String!, $rvms: [String!]) {
      parallaxMessages(vehicleId: $vehicleId, rvms: $rvms) {
        payload timestamp rvm
      }
    }
"""
MAX_CORRELATION_FRAMES = 5000


class ProbeHalt(RuntimeError):
    """Stop the probe without exposing the underlying exception."""


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="Capture sanitized API data")
    capture.add_argument("--label", required=True, help="State label for this capture")
    capture.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Ignored directory for sanitized captures",
    )
    capture.add_argument(
        "--config-entries",
        type=Path,
        default=CONFIG_ENTRIES_PATH,
        help=argparse.SUPPRESS,
    )
    capture.add_argument("--entry-id", help="Rivian config entry ID")
    capture.add_argument(
        "--query-set",
        action="append",
        choices=[*sorted(READ_ONLY_QUERY_SETS), "all"],
        dest="query_sets",
        help="Read-only query set; repeat to select more than one",
    )
    capture.add_argument(
        "--subscription-seconds",
        type=int,
        default=20,
        help="Bounded observation window for each subscription (5-120 seconds)",
    )

    correlate = subparsers.add_parser(
        "correlate",
        help="Interactively correlate manual state changes on one WebSocket",
    )
    correlate.add_argument(
        "--label", required=True, help="Label for the complete correlation session"
    )
    correlate.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Ignored directory for sanitized captures",
    )
    correlate.add_argument(
        "--config-entries",
        type=Path,
        default=CONFIG_ENTRIES_PATH,
        help=argparse.SUPPRESS,
    )
    correlate.add_argument("--entry-id", help="Rivian config entry ID")
    correlate.add_argument(
        "--vehicle-id",
        help="Vehicle ID; required only when the account has multiple vehicles",
    )
    correlate.add_argument(
        "--settle-seconds",
        type=int,
        default=4,
        help="Seconds to collect updates after each manual transition (1-15)",
    )

    subparsers.add_parser("self-test", help="Verify output redaction")
    return parser.parse_args()


def load_rivian_entry(path: Path, entry_id: str | None) -> dict[str, Any]:
    """Load one Rivian config entry from Home Assistant storage."""
    try:
        storage = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ProbeHalt(
            f"No Home Assistant config-entry storage found at {path}. "
            "Complete local Home Assistant and Rivian setup first."
        ) from err
    except (json.JSONDecodeError, OSError) as err:
        raise ProbeHalt(
            "Home Assistant config-entry storage could not be read."
        ) from err

    entries = [
        entry
        for entry in storage.get("data", {}).get("entries", [])
        if entry.get("domain") == "rivian"
        and (entry_id is None or entry.get("entry_id") == entry_id)
    ]
    if not entries:
        raise ProbeHalt("No matching Rivian config entry was found.")
    if len(entries) > 1:
        raise ProbeHalt("Multiple Rivian entries found; select one with --entry-id.")

    entry = entries[0]
    data = entry.get("data", {})
    missing = {
        key
        for key in ("access_token", "refresh_token", "user_session_token")
        if not data.get(key)
    }
    if missing:
        raise ProbeHalt("The selected Rivian entry is missing required session data.")
    return entry


def known_secrets(entry: Mapping[str, Any]) -> set[str]:
    """Return secret strings that must not survive serialization."""
    data = entry.get("data", {})
    return {
        value
        for key, value in data.items()
        if key in SECRET_ENTRY_KEYS and isinstance(value, str) and value
    }


def is_sensitive_key(key: str) -> bool:
    """Return whether a response key carries authentication material."""
    key_lower = key.lower()
    return (
        key_lower in AUTH_SECRET_KEYS_LOWER
        or key_lower.endswith("token")
        or key_lower.endswith("secret")
        or key_lower.endswith("privatekey")
        or key_lower.endswith("password")
    )


def sanitize(
    value: Any,
    secrets: Iterable[str] = (),
    path: tuple[str, ...] = (),
) -> Any:
    """Recursively redact authentication secrets while preserving API shapes."""
    secret_values = tuple(secret for secret in secrets if secret)

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            item_path = (*path, key)
            if is_sensitive_key(key):
                result[key] = REDACTED
            else:
                result[key] = sanitize(item, secret_values, item_path)
        return result

    if isinstance(value, list):
        return [sanitize(item, secret_values, (*path, "[]")) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item, secret_values, (*path, "[]")) for item in value]
    if isinstance(value, set):
        return sorted(sanitize(item, secret_values, (*path, "[]")) for item in value)
    if isinstance(value, str):
        sanitized = value
        for secret in secret_values:
            sanitized = sanitized.replace(secret, REDACTED)
        sanitized = AUTH_QUERY_VALUE_PATTERN.sub(rf"\1{REDACTED}", sanitized)
        return sanitized
    return value


def sanitizer_self_test() -> None:
    """Fail closed on auth secrets and preserve identifying research data."""
    canaries = {
        "access_token": "secret-access-token",
        "refresh_token": "secret-refresh-token",
        "session_token": "secret-session-token",
        "password": "secret-password",
        "vin": "7FCTGAAA0RN000000",
        "email": "driver@example.invalid",
        "name": "Private Vehicle Name",
        "latitude": "12.345678",
    }
    payload = {
        "accessToken": canaries["access_token"],
        "refreshToken": canaries["refresh_token"],
        "headers": {
            "Authorization": canaries["session_token"],
            "Cookie": canaries["session_token"],
        },
        "password": canaries["password"],
        "currentUser": {
            "email": canaries["email"],
            "vehicles": [
                {
                    "vin": canaries["vin"],
                    "name": canaries["name"],
                    "vehicle": {
                        "model": "R2",
                        "vehicleState": {
                            "gnssLocation": {"latitude": canaries["latitude"]},
                            "activeDriverName": {
                                "value": canaries["name"],
                                "timeStamp": "2026-01-01T00:00:00Z",
                            },
                            "supportedFeatures": [
                                {"name": "FEATURE_CHARGE_PORT", "status": "AVAILABLE"}
                            ],
                        },
                    },
                }
            ],
        },
        "error": f"Authentication failed for {canaries['email']}",
        "signedUrl": (
            "https://example.invalid/file?X-Amz-Credential=temporary-credential"
            "&X-Amz-Security-Token=temporary-security-token"
            "&X-Amz-Signature=temporary-signature&locale=en-US"
        ),
    }
    auth_secrets = {
        canaries["access_token"],
        canaries["refresh_token"],
        canaries["session_token"],
        canaries["password"],
    }
    encoded = json.dumps(sanitize(payload, auth_secrets))
    survivors = [secret for secret in auth_secrets if secret in encoded]
    if survivors:
        raise ProbeHalt("Sanitizer self-test failed; no capture was performed.")
    research_values = {
        canaries["vin"],
        canaries["email"],
        canaries["name"],
        canaries["latitude"],
        "FEATURE_CHARGE_PORT",
        "R2",
    }
    if any(value not in encoded for value in research_values):
        raise ProbeHalt("Sanitizer self-test removed identifying research data.")
    for signed_secret in (
        "temporary-credential",
        "temporary-security-token",
        "temporary-signature",
    ):
        if signed_secret in encoded:
            raise ProbeHalt("Sanitizer self-test retained signed URL credentials.")
    normalized_query = " ".join(PARALLAX_SUBSCRIPTION_QUERY.lower().split())
    if (
        not normalized_query.startswith("subscription ")
        or "mutation " in normalized_query
    ):
        raise ProbeHalt("Correlation query is not a read-only subscription.")


def safe_exception(error: Exception) -> dict[str, Any]:
    """Summarize an exception without rendering secret-bearing arguments."""
    result: dict[str, Any] = {"type": type(error).__name__}
    if len(error.args) > 1 and isinstance(error.args[1], int):
        result["http_status"] = error.args[1]
    if len(error.args) > 2 and isinstance(error.args[2], Mapping):
        errors = error.args[2].get("errors")
        if errors:
            result["graphql_errors"] = sanitize(errors)
    return result


def error_record(
    operation: str,
    error: Exception,
    requested_fields: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a safe operation record for a failed request."""
    fields = sorted(requested_fields)
    return {
        "operation": operation,
        "requested_fields": fields,
        "classification": classification(fields, None) if fields else {},
        "error": safe_exception(error),
    }


def classification(fields: Iterable[str], data: Any) -> dict[str, str]:
    """Classify requested fields by their response presence."""
    if not isinstance(data, Mapping):
        return {field: "response_unavailable" for field in sorted(fields)}
    statuses: dict[str, str] = {}
    for field in sorted(fields):
        if field not in data:
            statuses[field] = "omitted"
        elif data[field] is None:
            statuses[field] = "present_null"
        elif (
            isinstance(data[field], Mapping)
            and "value" in data[field]
            and data[field]["value"] is None
        ):
            statuses[field] = "present_null"
        else:
            statuses[field] = "present"
    return statuses


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Read one unsigned protobuf varint."""
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def decode_protobuf_wire(data: bytes) -> list[dict[str, Any]]:
    """Decode protobuf wire values without assuming an unverified schema."""
    fields: list[dict[str, Any]] = []
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 7
        field: dict[str, Any] = {
            "field_number": field_number,
            "wire_type": wire_type,
        }
        if wire_type == 0:
            field["value"], offset = _read_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ValueError("truncated 64-bit protobuf field")
            field["value"] = struct.unpack_from("<d", data, offset)[0]
            offset += 8
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated length-delimited protobuf field")
            value = data[offset:end]
            field["length"] = length
            field["value_b64"] = base64.b64encode(value).decode()
            try:
                decoded_text = value.decode("utf-8")
            except UnicodeDecodeError:
                decoded_text = None
            if decoded_text is not None and decoded_text.isprintable():
                field["text"] = decoded_text
            offset = end
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValueError("truncated 32-bit protobuf field")
            field["value"] = struct.unpack_from("<f", data, offset)[0]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.append(field)
    return fields


def _wire_value(fields: list[dict[str, Any]], number: int) -> Any:
    """Return the first decoded value for a protobuf field number."""
    return next(
        (field.get("value") for field in fields if field["field_number"] == number),
        None,
    )


def _wire_messages(
    fields: list[dict[str, Any]], number: int
) -> list[list[dict[str, Any]]]:
    """Decode all length-delimited values for one field as nested messages."""
    messages = []
    for field in fields:
        if field["field_number"] != number or "value_b64" not in field:
            continue
        try:
            messages.append(decode_protobuf_wire(base64.b64decode(field["value_b64"])))
        except ValueError:
            continue
    return messages


def decode_parallax_payload(rvm: str, payload: str) -> dict[str, Any]:
    """Decode known Parallax payloads and retain generic wire evidence."""
    try:
        raw = base64.b64decode(payload, validate=True)
        fields = decode_protobuf_wire(raw)
    except (ValueError, struct.error) as err:
        return {"decode_error": str(err), "payload_b64": payload}

    decoded: dict[str, Any] = {
        "byte_length": len(raw),
        "payload_b64": payload,
        "wire_fields": fields,
    }
    if rvm == "energy_edge_compute.graphs.charge_session_breakdown":
        names = {
            1: "total_kwh",
            2: "pack_kwh",
            3: "thermal_kwh",
            4: "outlets_kwh",
            5: "system_kwh",
            6: "session_duration_mins",
            7: "time_remaining_mins",
            8: "range_added_kms",
            9: "current_power_kw",
            10: "current_range_per_hour",
            12: "is_free_session",
            13: "charging_state",
        }
        decoded["interpreted"] = {
            name: _wire_value(fields, number) for number, name in names.items()
        }
    elif rvm == "charging.session.status":
        decoded["interpreted"] = {
            "plug_connection_status": _wire_value(fields, 1),
            "display_status": _wire_value(fields, 2),
            "evse_type": _wire_value(fields, 3),
        }
    elif rvm == "charging.session.time_estimation":
        decoded["interpreted"] = {
            "field_1": _wire_value(fields, 1),
            "estimated_minutes_remaining_observed": _wire_value(fields, 2),
        }
    elif rvm == "energy_edge_compute.graphs.charging_graph_global":
        bars = []
        for bar_fields in _wire_messages(fields, 1):
            bars.append(
                {
                    "soc": _wire_value(bar_fields, 1),
                    "power_kw": _wire_value(bar_fields, 2),
                    "start_time_ms": _wire_value(bar_fields, 3),
                    "end_time_ms": _wire_value(bar_fields, 4),
                    "time_estimation_validity_status": _wire_value(bar_fields, 5),
                    "charging_state": _wire_value(bar_fields, 6),
                    "bar_context": _wire_value(bar_fields, 7),
                }
            )
        decoded["interpreted"] = {"bars": bars}
    elif rvm == "energy.high_voltage.battery_state":
        charge_states = _wire_messages(fields, 1)
        temperature_states = _wire_messages(fields, 2)
        charge_state = charge_states[0] if charge_states else []
        temperature_state = temperature_states[0] if temperature_states else []
        decoded["interpreted"] = {
            "soc_percent": _wire_value(charge_state, 1),
            "pack_kwh": _wire_value(charge_state, 2),
            "range_km": _wire_value(charge_state, 3),
            "cell_average_c": _wire_value(temperature_state, 1),
            "cell_max_c": _wire_value(temperature_state, 2),
            "cell_min_c": _wire_value(temperature_state, 3),
            "power_output": _wire_value(fields, 4),
            "requires_calibration": _wire_value(fields, 5),
            "cold_weather_state": _wire_value(fields, 6),
        }
    elif rvm == "dynamics.tires.state":
        tires = []
        for tire_fields in _wire_messages(fields, 2):
            tires.append(
                {
                    "position": _wire_value(tire_fields, 1),
                    "status": _wire_value(tire_fields, 2),
                    "pressure_bar": _wire_value(tire_fields, 3),
                    "validity": _wire_value(tire_fields, 4),
                    "timestamp_ms": _wire_value(tire_fields, 5),
                }
            )
        decoded["interpreted"] = {
            "tpms_monitor_status": _wire_value(fields, 1),
            "tires": tires,
        }
    elif rvm in {"body.closures.states", "body.locks.states"}:
        states = []
        for state_fields in _wire_messages(fields, 1):
            states.append(
                {
                    "position": _wire_value(state_fields, 1),
                    "state": _wire_value(state_fields, 2),
                    "field_3": _wire_value(state_fields, 3),
                    "field_4": _wire_value(state_fields, 4),
                    "field_5": _wire_value(state_fields, 5),
                    "field_6": _wire_value(state_fields, 6),
                    "field_7": _wire_value(state_fields, 7),
                }
            )
        decoded["interpreted"] = {"states": states}
    elif rvm == "body.windows.states":
        states = []
        for state_fields in _wire_messages(fields, 1):
            states.append(
                {
                    "position": _wire_value(state_fields, 1),
                    "state": _wire_value(state_fields, 2),
                    "field_3": _wire_value(state_fields, 3),
                    "field_4": _wire_value(state_fields, 4),
                    "field_5": _wire_value(state_fields, 5),
                }
            )
        decoded["interpreted"] = {"states": states}
    elif rvm in {"dynamics.vehicle.drive_mode", "dynamics.vehicle.gear"}:
        decoded["interpreted"] = {"enum_value": _wire_value(fields, 1)}
    elif rvm == "comfort.cabin.cabin_preconditioning_status":
        decoded["interpreted"] = {
            "status": _wire_value(fields, 1),
            "type": _wire_value(fields, 2),
        }
    elif rvm == "comfort.cabin.cabin_temperatures":
        decoded["interpreted"] = {
            "field_1_c": _wire_value(fields, 1),
            "field_2_c": _wire_value(fields, 2),
            "field_3_c": _wire_value(fields, 3),
        }
    elif rvm == "comfort.cabin.hvac_settings_status":
        decoded["interpreted"] = {"cabin_target_c": _wire_value(fields, 1)}
    elif rvm == "vehicle.power.state":
        decoded["interpreted"] = {"power_state": _wire_value(fields, 1)}
    return decoded


def response_record(
    operation: str,
    status: int,
    payload: Mapping[str, Any],
    requested_fields: Iterable[str] = (),
    root_field: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a consistent operation record."""
    fields = sorted(requested_fields)
    root_data = payload.get("data", {})
    root_path = (root_field,) if isinstance(root_field, str) else root_field or ()
    for path_part in root_path:
        if not isinstance(root_data, Mapping):
            break
        root_data = root_data.get(path_part)
    return {
        "operation": operation,
        "http_status": status,
        "requested_fields": fields,
        "classification": classification(fields, root_data) if fields else {},
        "response": payload,
    }


def vehicle_field_analysis(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Classify every known vehicle-state field and retain timestamp evidence."""
    observations: dict[str, list[Mapping[str, Any]]] = {}
    for frame in record.get("response", {}).get("frames", []):
        vehicle_state = frame.get("data", {}).get("vehicleState")
        if not isinstance(vehicle_state, Mapping):
            continue
        for field, value in vehicle_state.items():
            if value is not None:
                observations.setdefault(field, []).append(value)

    captured_at = datetime.now(UTC)
    analysis = {}
    for field in sorted(VEHICLE_STATES_SUBSCRIPTION_PROPERTIES):
        status = record.get("classification", {}).get(field, "response_unavailable")
        timestamps = []
        for observation in observations.get(field, []):
            if not isinstance(observation, Mapping):
                continue
            timestamp = observation.get("timeStamp") or observation.get("updatedAt")
            if isinstance(timestamp, str):
                timestamps.append(timestamp)
        latest_timestamp = max(timestamps, default=None)
        age_seconds = None
        freshness = "unknown"
        if latest_timestamp:
            try:
                observed_at = datetime.fromisoformat(
                    latest_timestamp.replace("Z", "+00:00")
                )
                age_seconds = max(0, int((captured_at - observed_at).total_seconds()))
                freshness = "current" if age_seconds <= 300 else "stale"
            except ValueError:
                freshness = "unparseable_timestamp"

        category = status
        if status == "present":
            category = "present_stale" if freshness == "stale" else "present_r2_value"
        analysis[field] = {
            "category": category,
            "requested_by_integration": field in VEHICLE_STATE_API_FIELDS,
            "latest_timestamp": latest_timestamp,
            "age_seconds": age_seconds,
            "freshness": freshness,
        }
    return analysis


def check_halt_errors(payload: Mapping[str, Any]) -> None:
    """Stop on API responses that indicate rate limiting or account risk."""
    codes = {
        error.get("extensions", {}).get("code")
        for error in payload.get("errors", [])
        if isinstance(error, Mapping)
    }
    if codes & {"RATE_LIMIT", "RATE_LIMITED", "TOO_MANY_REQUESTS"}:
        raise ProbeHalt("Rivian rate limiting was reported; probing stopped.")
    if codes & {"ACCOUNT_LOCKED", "SESSION_MANAGER_ERROR", "TEMPORARILY_LOCKED"}:
        raise ProbeHalt("Rivian session locking was reported; probing stopped.")
    if "UNAUTHENTICATED" in codes:
        raise ProbeHalt("The Rivian session is no longer authenticated.")


async def public_operation(
    operation: str,
    request: Awaitable[aiohttp.ClientResponse],
    requested_fields: Iterable[str] = (),
    root_field: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Capture a public rivian-python-client request safely."""
    try:
        response = await request
        payload = await response.json()
        check_halt_errors(payload)
        return response_record(
            operation,
            response.status,
            payload,
            requested_fields,
            root_field,
        )
    except RivianApiRateLimitError as err:
        raise ProbeHalt("Rivian rate limiting was reported; probing stopped.") from err
    except RivianTemporarilyLockedError as err:
        raise ProbeHalt(
            "Rivian session locking was reported; probing stopped."
        ) from err
    except RivianUnauthenticated as err:
        raise ProbeHalt("The Rivian session is no longer authenticated.") from err
    except Exception as err:  # pylint: disable=broad-except
        return error_record(operation, err, requested_fields)


def authenticated_headers(client: Rivian) -> dict[str, str]:
    """Build authenticated headers without returning them in probe output."""
    return BASE_HEADERS | {
        "Csrf-Token": client._csrf_token,  # pylint: disable=protected-access
        "A-Sess": client._app_session_token,  # pylint: disable=protected-access
        "U-Sess": client._user_session_token,  # pylint: disable=protected-access
    }


async def graphql_operation(
    session: aiohttp.ClientSession,
    client: Rivian,
    *,
    endpoint: str,
    operation: str,
    query: str,
    variables: Mapping[str, Any] | None = None,
    requested_fields: Iterable[str] = (),
    root_field: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Run a one-shot read-only GraphQL query."""
    body = {"operationName": operation, "query": query, "variables": variables}
    try:
        async with session.post(
            endpoint,
            json=body,
            headers=authenticated_headers(client),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            payload = await response.json()
            if response.status == 429:
                raise ProbeHalt("Rivian rate limiting was reported; probing stopped.")
            check_halt_errors(payload)
            return response_record(
                operation,
                response.status,
                payload,
                requested_fields,
                root_field,
            )
    except ProbeHalt:
        raise
    except Exception as err:  # pylint: disable=broad-except
        return error_record(operation, err, requested_fields)


async def subscription_operation(
    session: aiohttp.ClientSession,
    client: Rivian,
    *,
    operation: str,
    query: str,
    variables: Mapping[str, Any],
    graphql_operation_name: str | None = None,
    requested_fields: Iterable[str] = (),
    root_field: str | tuple[str, ...] | None = None,
    capture_seconds: int = 0,
    max_frames: int = 100,
) -> dict[str, Any]:
    """Capture one or a bounded series of read-only subscription updates."""
    subscription_id = str(uuid4())
    try:
        async with session.ws_connect(
            GRAPHQL_WEBSOCKET,
            headers={"sec-websocket-protocol": "graphql-transport-ws"},
            timeout=aiohttp.ClientWSTimeout(ws_receive=20, ws_close=5),
        ) as websocket:
            await websocket.send_json(
                {
                    "payload": {
                        "client-name": APOLLO_CLIENT_NAME,
                        "client-version": "1.13.0-1494",
                        "dc-cid": f"m-ios-{uuid4()}",
                        "u-sess": client._user_session_token,  # pylint: disable=protected-access
                    },
                    "type": "connection_init",
                }
            )
            while True:
                message = await websocket.receive_json(timeout=15)
                if message.get("type") == "connection_ack":
                    break
                if message.get("type") in {"error", "connection_error"}:
                    payload = {"errors": message.get("payload", [])}
                    check_halt_errors(payload)
                    return response_record(
                        operation,
                        101,
                        payload,
                        requested_fields,
                        root_field,
                    )

            await websocket.send_json(
                {
                    "id": subscription_id,
                    "payload": {
                        "operationName": graphql_operation_name or operation,
                        "query": query,
                        "variables": dict(variables),
                    },
                    "type": "subscribe",
                }
            )

            payloads: list[Mapping[str, Any]] = []
            deadline = (
                asyncio.get_running_loop().time() + capture_seconds
                if capture_seconds
                else None
            )
            while True:
                timeout = 15.0
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    timeout = min(timeout, remaining)
                try:
                    message = await websocket.receive_json(timeout=timeout)
                except TimeoutError:
                    if payloads:
                        break
                    raise
                message_type = message.get("type")
                if message_type == "ping":
                    await websocket.send_json(
                        {"type": "pong", "payload": message.get("payload")}
                    )
                    continue
                if message_type == "next":
                    payload = message.get("payload", {})
                    check_halt_errors(payload)
                    payloads.append(payload)
                    if deadline is None:
                        return response_record(
                            operation,
                            101,
                            payload,
                            requested_fields,
                            root_field,
                        )
                    if len(payloads) >= max_frames:
                        break
                    continue
                if message_type in {"error", "complete"}:
                    message_payload = message.get("payload", [])
                    payload = {
                        "errors": (
                            message_payload
                            if isinstance(message_payload, list)
                            else [message_payload]
                        )
                    }
                    check_halt_errors(payload)
                    return response_record(
                        operation,
                        101,
                        payload,
                        requested_fields,
                        root_field,
                    )
            merged: dict[str, Any] = {}
            root_path = (
                (root_field,) if isinstance(root_field, str) else root_field or ()
            )
            for payload in payloads:
                root_data: Any = payload.get("data", {})
                for path_part in root_path:
                    if not isinstance(root_data, Mapping):
                        break
                    root_data = root_data.get(path_part)
                if isinstance(root_data, Mapping):
                    for key, value in root_data.items():
                        record_value = (
                            value.get("value") if isinstance(value, Mapping) else value
                        )
                        if key not in merged or record_value is not None:
                            merged[key] = value
            fields = sorted(requested_fields)
            return {
                "operation": operation,
                "http_status": 101,
                "requested_fields": fields,
                "classification": classification(fields, merged) if fields else {},
                "frame_count": len(payloads),
                "response": {"frames": payloads},
            }
    except ProbeHalt:
        raise
    except Exception as err:  # pylint: disable=broad-except
        return error_record(operation, err, requested_fields)


async def pause() -> None:
    """Avoid sending bursts of requests to the private API."""
    await asyncio.sleep(1)


async def capture_vehicle_state(
    session: aiohttp.ClientSession,
    client: Rivian,
    vehicle_id: str,
    subscription_seconds: int,
) -> list[dict[str, Any]]:
    """Capture all known fields once and classify the integration subset."""
    fields = VEHICLE_STATES_SUBSCRIPTION_PROPERTIES
    fragment = client._build_vehicle_state_fragment(fields)  # pylint: disable=protected-access
    query = (
        "subscription VehicleState($vehicleID: String!) { "
        f"vehicleState(id: $vehicleID) {fragment} }}"
    )
    known_record = await subscription_operation(
        session,
        client,
        operation="VehicleStateKnownFields",
        graphql_operation_name="VehicleState",
        query=query,
        variables={"vehicleID": vehicle_id},
        requested_fields=fields,
        root_field="vehicleState",
        capture_seconds=subscription_seconds,
    )
    known_record["field_analysis"] = vehicle_field_analysis(known_record)
    integration_record = {
        **known_record,
        "operation": "VehicleStateIntegrationFieldsDerived",
        "requested_fields": sorted(VEHICLE_STATE_API_FIELDS),
        "classification": {
            field: known_record.get("classification", {}).get(
                field, "response_unavailable"
            )
            for field in sorted(VEHICLE_STATE_API_FIELDS)
        },
        "derived_from": "VehicleStateKnownFields",
    }
    return [known_record, integration_record]


async def capture_parallax(
    session: aiohttp.ClientSession,
    client: Rivian,
    vehicle_id: str,
    subscription_seconds: int,
) -> dict[str, Any]:
    """Capture and decode a bounded sample from modern Parallax telemetry."""
    record = await subscription_operation(
        session,
        client,
        operation="ParallaxMessages",
        query=PARALLAX_SUBSCRIPTION_QUERY,
        variables={"vehicleId": vehicle_id, "rvms": list(PARALLAX_RVMS)},
        root_field="parallaxMessages",
        capture_seconds=subscription_seconds,
        max_frames=250,
    )
    record["requested_rvms"] = list(PARALLAX_RVMS)
    frames = record.get("response", {}).get("frames", [])
    decoded_frames = []
    observed_rvms = set()
    for frame in frames:
        message = frame.get("data", {}).get("parallaxMessages")
        if not isinstance(message, Mapping):
            continue
        rvm = message.get("rvm")
        payload = message.get("payload")
        if not isinstance(rvm, str) or not isinstance(payload, str):
            continue
        observed_rvms.add(rvm)
        decoded_frames.append(
            {
                "rvm": rvm,
                "timestamp": message.get("timestamp"),
                "decoded": decode_parallax_payload(rvm, payload),
            }
        )
    record["observed_rvms"] = sorted(observed_rvms)
    record["unobserved_rvms"] = sorted(set(PARALLAX_RVMS) - observed_rvms)
    record["decoded_frames"] = decoded_frames
    return record


def correlation_frame(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize one Parallax frame without assigning semantic enum labels."""
    parallax = message.get("data", {}).get("parallaxMessages")
    if not isinstance(parallax, Mapping):
        return None
    rvm = parallax.get("rvm")
    payload = parallax.get("payload")
    if not isinstance(rvm, str) or not isinstance(payload, str):
        return None
    return {
        "received_at": datetime.now(UTC).isoformat(),
        "rvm": rvm,
        "vehicle_timestamp": parallax.get("timestamp"),
        "decoded": decode_parallax_payload(rvm, payload),
    }


def correlation_snapshot(
    frames: list[dict[str, Any]], end_index: int | None = None
) -> dict[str, dict[str, Any]]:
    """Return the last frame observed for each RVM before an index."""
    snapshot: dict[str, dict[str, Any]] = {}
    for frame in frames[:end_index]:
        snapshot[frame["rvm"]] = frame
    return snapshot


def evidence_view(frame: Mapping[str, Any] | None) -> Any:
    """Return decoded evidence without duplicating the raw payload in a delta."""
    if frame is None:
        return None
    decoded = frame.get("decoded")
    if not isinstance(decoded, Mapping):
        return decoded
    return {key: value for key, value in decoded.items() if key != "payload_b64"}


def structural_changes(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Describe changed decoded paths without interpreting their meaning."""
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes = []
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}.{key}" if path else str(key)
            changes.extend(
                structural_changes(before.get(key), after.get(key), child_path)
            )
        return changes
    if before == after:
        return []
    return [{"path": path or "$", "before": before, "after": after}]


def correlation_deltas(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return before/after evidence for every RVM whose payload changed."""
    deltas = {}
    for rvm in sorted(set(before) | set(after)):
        before_frame = before.get(rvm)
        after_frame = after.get(rvm)
        before_payload = (
            before_frame.get("decoded", {}).get("payload_b64") if before_frame else None
        )
        after_payload = (
            after_frame.get("decoded", {}).get("payload_b64") if after_frame else None
        )
        if before_payload == after_payload:
            continue
        before_evidence = evidence_view(before_frame)
        after_evidence = evidence_view(after_frame)
        deltas[rvm] = {
            "before": before_evidence,
            "after": after_evidence,
            "changes": structural_changes(before_evidence, after_evidence),
        }
    return deltas


async def prompt_user(prompt: str) -> str:
    """Read terminal input without blocking WebSocket processing."""
    try:
        return await asyncio.to_thread(input, prompt)
    except EOFError as err:
        raise ProbeHalt(
            "Interactive input ended before the session was complete."
        ) from err


async def correlation_listener(
    websocket: aiohttp.ClientWebSocketResponse,
    frames: list[dict[str, Any]],
) -> None:
    """Continuously collect read-only Parallax subscription frames."""
    while not websocket.closed:
        try:
            ws_message = await websocket.receive(timeout=60)
        except TimeoutError:
            continue
        if ws_message.type in {
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
        }:
            raise ProbeHalt("The Parallax WebSocket closed during correlation.")
        if ws_message.type is aiohttp.WSMsgType.ERROR:
            raise ProbeHalt("The Parallax WebSocket reported a transport error.")
        if ws_message.type not in {
            aiohttp.WSMsgType.TEXT,
            aiohttp.WSMsgType.BINARY,
        }:
            continue
        try:
            message = json.loads(ws_message.data)
        except (TypeError, ValueError):
            continue
        if not isinstance(message, Mapping):
            continue
        message_type = message.get("type")
        if message_type == "ping":
            await websocket.send_json(
                {"type": "pong", "payload": message.get("payload")}
            )
            continue
        if message_type == "next":
            payload = message.get("payload", {})
            check_halt_errors(payload)
            if frame := correlation_frame(payload):
                frames.append(frame)
                if len(frames) >= MAX_CORRELATION_FRAMES:
                    raise ProbeHalt(
                        "Correlation frame limit reached; finish with a shorter session."
                    )
            continue
        if message_type in {"error", "connection_error"}:
            message_payload = message.get("payload", [])
            errors = (
                message_payload
                if isinstance(message_payload, list)
                else [message_payload]
            )
            check_halt_errors({"errors": errors})
            raise ProbeHalt("The Parallax subscription reported an API error.")
        if message_type == "complete":
            raise ProbeHalt("The Parallax subscription ended unexpectedly.")


def check_listener(listener: asyncio.Task[None]) -> None:
    """Propagate a listener failure without exposing secret-bearing details."""
    if not listener.done():
        return
    if listener.cancelled():
        raise ProbeHalt("The Parallax subscription was cancelled unexpectedly.")
    error = listener.exception()
    if isinstance(error, ProbeHalt):
        raise error
    if error is not None:
        raise ProbeHalt(
            f"The Parallax subscription failed ({type(error).__name__})."
        ) from error
    raise ProbeHalt("The Parallax subscription ended unexpectedly.")


async def interactive_parallax_correlation(
    session: aiohttp.ClientSession,
    client: Rivian,
    vehicle_id: str,
    settle_seconds: int,
) -> dict[str, Any]:
    """Correlate labeled physical transitions on one read-only WebSocket."""
    baseline_label = (
        await prompt_user("Baseline label [baseline-all-closed-locked-park]: ")
    ).strip() or "baseline-all-closed-locked-park"
    if not LABEL_PATTERN.fullmatch(baseline_label):
        raise ProbeHalt(
            "Baseline label must contain only lowercase letters, digits, hyphens, "
            "or underscores."
        )
    await prompt_user(
        "Put the stationary vehicle in that baseline state, keep Home Assistant "
        "stopped, then press Enter to open the read-only subscription. "
    )

    subscription_id = str(uuid4())
    frames: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    baseline_end = 0
    interrupted_reason: str | None = None
    used_labels = {baseline_label}
    try:
        async with session.ws_connect(
            GRAPHQL_WEBSOCKET,
            headers={"sec-websocket-protocol": "graphql-transport-ws"},
            timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=5),
        ) as websocket:
            await websocket.send_json(
                {
                    "payload": {
                        "client-name": APOLLO_CLIENT_NAME,
                        "client-version": "1.13.0-1494",
                        "dc-cid": f"m-ios-{uuid4()}",
                        "u-sess": client._user_session_token,  # pylint: disable=protected-access
                    },
                    "type": "connection_init",
                }
            )
            while True:
                message = await websocket.receive_json(timeout=15)
                if message.get("type") == "connection_ack":
                    break
                if message.get("type") in {"error", "connection_error"}:
                    raise ProbeHalt(
                        "The Parallax WebSocket rejected the stored Rivian session."
                    )

            await websocket.send_json(
                {
                    "id": subscription_id,
                    "payload": {
                        "operationName": "ParallaxMessages",
                        "query": PARALLAX_SUBSCRIPTION_QUERY,
                        "variables": {
                            "vehicleId": vehicle_id,
                            "rvms": list(CORRELATION_RVMS),
                        },
                    },
                    "type": "subscribe",
                }
            )
            listener = asyncio.create_task(
                correlation_listener(websocket, frames),
                name="rivian-parallax-correlation",
            )
            try:
                await asyncio.sleep(max(5, settle_seconds))
                check_listener(listener)
                if not frames:
                    raise ProbeHalt(
                        "No Parallax frames arrived. Stop Home Assistant so it does "
                        "not compete for the account WebSocket, then retry."
                    )
                baseline_end = len(frames)
                print(
                    f"Baseline captured with {baseline_end} frame(s). Keep the "
                    "vehicle unchanged while entering each next label.",
                    flush=True,
                )
                print(
                    "Use one physical transition per label; enter 'done' when "
                    "finished. No enum or position mapping is accepted merely from "
                    "a label.",
                    flush=True,
                )

                while True:
                    check_listener(listener)
                    transition_label = (
                        await prompt_user("Next transition label (or done): ")
                    ).strip()
                    if transition_label == "done":
                        break
                    if not LABEL_PATTERN.fullmatch(transition_label):
                        print(
                            "Use lowercase letters, digits, hyphens, or underscores.",
                            flush=True,
                        )
                        continue
                    if transition_label in used_labels:
                        print("That label was already used.", flush=True)
                        continue

                    action_start = len(frames)
                    before = correlation_snapshot(frames, action_start)
                    started_at = datetime.now(UTC).isoformat()
                    await prompt_user(
                        "Perform exactly that one manual transition now, then press "
                        "Enter when the vehicle has reached the labeled state. "
                    )
                    await asyncio.sleep(settle_seconds)
                    check_listener(listener)
                    action_end = len(frames)
                    after = correlation_snapshot(frames, action_end)
                    deltas = correlation_deltas(before, after)
                    transitions.append(
                        {
                            "candidate_label": transition_label,
                            "mapping_status": "candidate_unverified",
                            "started_at": started_at,
                            "completed_at": datetime.now(UTC).isoformat(),
                            "frame_start_index": action_start,
                            "frame_end_index": action_end,
                            "observed_frame_count": action_end - action_start,
                            "changed_rvms": sorted(deltas),
                            "deltas": deltas,
                            "evidence_gate": (
                                "Repeat the isolated transition and its return-to-"
                                "baseline before assigning semantic position or enum "
                                "labels."
                            ),
                        }
                    )
                    used_labels.add(transition_label)
                    changed = ", ".join(sorted(deltas)) or "none"
                    print(f"Observed changed RVMs: {changed}", flush=True)
            finally:
                if not websocket.closed:
                    with contextlib.suppress(Exception):
                        await websocket.send_json(
                            {"id": subscription_id, "type": "complete"}
                        )
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener
    except ProbeHalt as err:
        if not frames:
            raise
        interrupted_reason = str(err)
    except (RivianApiRateLimitError, RivianTemporarilyLockedError) as err:
        if frames:
            interrupted_reason = "Rivian rate or session locking stopped correlation."
        else:
            raise ProbeHalt(
                "Rivian rate or session locking stopped correlation."
            ) from err
    except RivianUnauthenticated as err:
        if frames:
            interrupted_reason = "The Rivian session is no longer authenticated."
        else:
            raise ProbeHalt("The Rivian session is no longer authenticated.") from err
    except Exception as err:  # pylint: disable=broad-except
        if frames:
            interrupted_reason = (
                f"The correlation WebSocket failed ({type(err).__name__})."
            )
        else:
            raise ProbeHalt(
                f"The correlation WebSocket failed ({type(err).__name__})."
            ) from err

    observed_rvms = {frame["rvm"] for frame in frames}
    return {
        "operation": "InteractiveParallaxCorrelation",
        "transport": "single_graphql_websocket",
        "read_only": True,
        "completed": interrupted_reason is None,
        "interrupted_reason": interrupted_reason,
        "requested_rvms": list(CORRELATION_RVMS),
        "observed_rvms": sorted(observed_rvms),
        "unobserved_rvms": sorted(set(CORRELATION_RVMS) - observed_rvms),
        "checklist": list(CORRELATION_CHECKLIST),
        "baseline": {
            "candidate_label": baseline_label,
            "mapping_status": "candidate_unverified",
            "frame_start_index": 0,
            "frame_end_index": baseline_end,
            "snapshot": correlation_snapshot(frames, baseline_end),
        },
        "transitions": transitions,
        "frame_count": len(frames),
        "frames": frames,
    }


async def capture_charging(
    session: aiohttp.ClientSession,
    client: Rivian,
    vehicle_id: str,
) -> list[dict[str, Any]]:
    """Capture legacy and subscription charging data."""
    records = [
        await public_operation(
            "GetLiveSessionIntegrationFields",
            client.get_live_charging_session(vehicle_id, CHARGING_API_FIELDS),
            CHARGING_API_FIELDS,
            "getLiveSessionData",
        )
    ]
    await pause()
    records.append(
        await public_operation(
            "GetLiveSessionKnownFields",
            client.get_live_charging_session(vehicle_id, LIVE_SESSION_PROPERTIES),
            LIVE_SESSION_PROPERTIES,
            "getLiveSessionData",
        )
    )
    await pause()

    records.append(
        await graphql_operation(
            session,
            client,
            endpoint=GRAPHQL_CHARGING,
            operation="GetSessionStatusShape",
            query="""
                query GetSessionStatusShape($vehicleId: ID!) {
                  getSessionStatus(vehicleId: $vehicleId) { __typename }
                }
            """,
            variables={"vehicleId": vehicle_id},
            root_field="getSessionStatus",
        )
    )
    await pause()

    live_fields = {
        "currency",
        "isFreeSession",
        "kilometersChargedPerHour",
        "powerKW",
        "price",
        "rangeAddedThisSession",
        "startTime",
        "timeElapsed",
        "timeRemaining",
        "totalChargedEnergy",
        "vehicleChargerState",
    }
    records.append(
        await subscription_operation(
            session,
            client,
            operation="ChargingSession",
            query="""
                subscription ChargingSession($vehicleId: String!) {
                  chargingSession(vehicleId: $vehicleId) {
                    chartData {
                      soc powerKW startTime endTime
                      timeEstimationValidityStatus vehicleChargerState
                    }
                    liveData {
                      powerKW kilometersChargedPerHour rangeAddedThisSession
                      totalChargedEnergy timeElapsed timeRemaining price currency
                      isFreeSession vehicleChargerState startTime
                    }
                  }
                }
            """,
            variables={"vehicleId": vehicle_id},
            requested_fields=live_fields,
            root_field=("chargingSession", "liveData"),
        )
    )
    await pause()
    records.extend(
        [
            await targeted_introspection(
                session, client, GRAPHQL_CHARGING, "LiveSessionData"
            ),
            await targeted_introspection(
                session, client, GRAPHQL_CHARGING, "ChargingSession"
            ),
        ]
    )
    return records


async def capture_history(
    session: aiohttp.ClientSession,
    client: Rivian,
    vehicle_id: str,
    user_id: str,
) -> list[dict[str, Any]]:
    """Capture available charging-history surfaces for the last 30 days."""
    now = datetime.now(UTC)
    start = now - timedelta(days=30)
    records = [
        await graphql_operation(
            session,
            client,
            endpoint=GRAPHQL_CHARGING,
            operation="CompletedSessionSummaries",
            query="""
                query CompletedSessionSummaries($vehicleId: String) {
                  getCompletedSessionSummaries(vehicleId: $vehicleId) {
                    chargerType currencyCode paidTotal startInstant endInstant
                    totalEnergyKwh rangeAddedKm city transactionId vehicleId
                    vehicleName vendor isRoamingNetwork isPublic isHomeCharger
                    meta { transactionIdGroupingKey dataSources }
                  }
                }
            """,
            variables={"vehicleId": vehicle_id},
            root_field="getCompletedSessionSummaries",
        )
    ]
    await pause()
    records.append(
        await graphql_operation(
            session,
            client,
            endpoint=GRAPHQL_CHARGING,
            operation="LiveSessionHistoryShape",
            query="""
                query LiveSessionHistoryShape($vehicleId: ID!) {
                  getLiveSessionHistory(vehicleId: $vehicleId) {
                    __typename
                    chartData { __typename time kw }
                    startTime vehicleId chargerId transactionId
                  }
                }
            """,
            variables={"vehicleId": vehicle_id},
            root_field="getLiveSessionHistory",
        )
    )
    await pause()
    records.append(
        await graphql_operation(
            session,
            client,
            endpoint=GRAPHQL_CHARGING,
            operation="LiveSessionHistoryCurrent",
            query="""
                query LiveSessionHistoryCurrent($vehicleId: ID!) {
                  getLiveSessionHistory(vehicleId: $vehicleId) {
                    current { value updatedAt }
                  }
                }
            """,
            variables={"vehicleId": vehicle_id},
            requested_fields={"current"},
            root_field="getLiveSessionHistory",
        )
    )
    await pause()
    records.append(
        await graphql_operation(
            session,
            client,
            endpoint=GRAPHQL_CHARGING,
            operation="SessionHistory",
            query="""
                query SessionHistory(
                  $userId: ID!, $startDate: String!, $endDate: String!
                ) {
                  getSessionHistory(
                    userId: $userId, startDate: $startDate, endDate: $endDate
                  ) {
                    transactionId locationId vehicleId startDateTime endDateTime
                    currency cost paidTotal energyOffered energyUnits rangeAdded
                    rangeUnits vendor chargerType chargerName address city country
                    postalCode
                  }
                }
            """,
            variables={
                "userId": user_id,
                "startDate": start.date().isoformat(),
                "endDate": now.date().isoformat(),
            },
            root_field="getSessionHistory",
        )
    )
    return records


async def targeted_introspection(
    session: aiohttp.ClientSession,
    client: Rivian,
    endpoint: str,
    type_name: str,
) -> dict[str, Any]:
    """Request a single GraphQL type rather than the entire schema."""
    return await graphql_operation(
        session,
        client,
        endpoint=endpoint,
        operation=f"Introspect{type_name}",
        query="""
            query IntrospectType($name: String!) {
              __type(name: $name) {
                name
                fields {
                  name
                  type { kind name ofType { kind name } }
                }
              }
            }
        """,
        variables={"name": type_name},
        root_field="__type",
    )


async def capture_ota_images(
    session: aiohttp.ClientSession,
    client: Rivian,
    vehicle_id: str,
) -> list[dict[str, Any]]:
    """Capture OTA, configuration, schedules, and mobile image metadata."""
    records = [
        await public_operation(
            "GetOTAUpdateDetailsCoupledClientQuery",
            client.get_vehicle_ota_update_details(vehicle_id),
            root_field="getVehicle",
        )
    ]
    await pause()
    records.append(
        await graphql_operation(
            session,
            client,
            endpoint=GRAPHQL_GATEWAY,
            operation="CurrentOTAUpdateDetails",
            query="""
                query CurrentOTAUpdateDetails($vehicleId: String!) {
                  getVehicle(id: $vehicleId) {
                    currentOTAUpdateDetails { url version locale }
                  }
                }
            """,
            variables={"vehicleId": vehicle_id},
            root_field=("getVehicle", "currentOTAUpdateDetails"),
        )
    )
    await pause()
    records.append(
        await graphql_operation(
            session,
            client,
            endpoint=GRAPHQL_GATEWAY,
            operation="AvailableOTAUpdateDetails",
            query="""
                query AvailableOTAUpdateDetails($vehicleId: String!) {
                  getVehicle(id: $vehicleId) {
                    availableOTAUpdateDetails { url version locale }
                  }
                }
            """,
            variables={"vehicleId": vehicle_id},
            root_field=("getVehicle", "availableOTAUpdateDetails"),
        )
    )
    await pause()
    vehicle_reads = {
        "VehicleConfiguration": """
            otaEarlyAccessStatus
            mobileConfiguration {
              trimOption { optionId optionName }
              driveSystemOption { optionId optionName }
              exteriorColorOption { optionId optionName }
              interiorColorOption { optionId optionName }
            }
        """,
        "VehicleSettings": "settings { name { value } }",
        "VehicleChargingSchedules": """
            chargingSchedules {
              startTime duration amperage enabled weekDays
              location { latitude longitude }
            }
        """,
        "VehicleEstimatedRange": "estimatedRange(startSoc: 100)",
    }
    for operation, selection in vehicle_reads.items():
        records.append(
            await graphql_operation(
                session,
                client,
                endpoint=GRAPHQL_GATEWAY,
                operation=operation,
                query=f"""
                    query {operation}($vehicleId: String!) {{
                      getVehicle(id: $vehicleId) {{ {selection} }}
                    }}
                """,
                variables={"vehicleId": vehicle_id},
                root_field="getVehicle",
            )
        )
        await pause()
    records.append(
        await graphql_operation(
            session,
            client,
            endpoint=GRAPHQL_GATEWAY,
            operation="ConnectedProducts",
            query="""
                query ConnectedProducts {
                  currentUser {
                    vehicles {
                      id
                      connectedProducts {
                        __typename
                        ... on CampSpeaker { id serialNumber }
                      }
                    }
                  }
                }
            """,
            root_field=("currentUser", "vehicles"),
        )
    )
    await pause()
    records.append(
        await public_operation(
            "GetVehicleImages",
            client.get_vehicle_images(resolution="@3x", vehicle_version="3"),
            root_field="getVehicleMobileImages",
        )
    )
    return records


def git_commit() -> str | None:
    """Return the current repository commit without failing the capture."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def home_assistant_snapshot(
    config_entries_path: Path, config_entry_id: str
) -> dict[str, Any]:
    """Read the matching local HA entity registry and latest recorder states."""
    registry_path = config_entries_path.with_name("core.entity_registry")
    database_candidates = (
        Path("/tmp/home-assistant-rivian-dev.db"),
        config_entries_path.parent.parent / "home-assistant_v2.db",
    )
    database_path = next(
        (candidate for candidate in database_candidates if candidate.exists()),
        database_candidates[-1],
    )
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"available": False, "reason": "entity registry unavailable"}

    entities = [
        {
            "device_id": entity.get("device_id"),
            "disabled_by": entity.get("disabled_by"),
            "entity_id": entity.get("entity_id"),
            "original_name": entity.get("original_name"),
        }
        for entity in registry.get("data", {}).get("entities", [])
        if entity.get("config_entry_id") == config_entry_id
    ]
    entity_ids = {entity["entity_id"] for entity in entities if entity.get("entity_id")}
    latest_states: dict[str, dict[str, Any]] = {}
    states_available = False
    states_reason = "recorder database unavailable"
    if database_path.exists() and entity_ids:
        try:
            with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as database:
                rows = database.execute(
                    """
                    WITH latest AS (
                      SELECT metadata_id, MAX(state_id) AS state_id
                      FROM states GROUP BY metadata_id
                    )
                    SELECT sm.entity_id, s.state, s.last_updated_ts
                    FROM latest
                    JOIN states s ON s.state_id = latest.state_id
                    JOIN states_meta sm ON sm.metadata_id = latest.metadata_id
                    """
                )
                for entity_id, state, updated_timestamp in rows:
                    if entity_id not in entity_ids:
                        continue
                    latest_states[entity_id] = {
                        "state": state,
                        "updated_at": (
                            datetime.fromtimestamp(updated_timestamp, UTC).isoformat()
                            if updated_timestamp is not None
                            else None
                        ),
                    }
                states_available = True
                states_reason = "available"
        except sqlite3.Error as err:
            latest_states = {}
            states_reason = f"recorder database error: {type(err).__name__}"

    for entity in entities:
        entity.update(latest_states.get(entity["entity_id"], {}))
    return {
        "available": True,
        "captured_at": datetime.now(UTC).isoformat(),
        "database_path": str(database_path),
        "entity_count": len(entities),
        "entities": sorted(entities, key=lambda item: item["entity_id"]),
        "states_available": states_available,
        "states_reason": states_reason,
    }


async def run_capture(args: argparse.Namespace) -> Path:
    """Run the requested read-only capture."""
    sanitizer_self_test()
    if not LABEL_PATTERN.fullmatch(args.label):
        raise ProbeHalt(
            "Label must contain only lowercase letters, digits, hyphens, or underscores."
        )
    if not 5 <= args.subscription_seconds <= 120:
        raise ProbeHalt("Subscription window must be between 5 and 120 seconds.")

    entry = load_rivian_entry(args.config_entries, args.entry_id)
    entry_data = entry["data"]
    secrets = known_secrets(entry)
    query_sets = set(args.query_sets or ["all"])
    if "all" in query_sets:
        query_sets = set(READ_ONLY_QUERY_SETS)

    report: dict[str, Any] = {
        "metadata": {
            "captured_at": datetime.now(UTC).isoformat(),
            "label": args.label,
            "query_sets": sorted(query_sets),
            "repository_commit": git_commit(),
            "homeassistant_version": version("homeassistant"),
            "rivian_python_client_version": version("rivian-python-client"),
        },
        "operations": [],
        "vehicles": [],
        "home_assistant_snapshot": home_assistant_snapshot(
            args.config_entries, entry["entry_id"]
        ),
    }

    async with aiohttp.ClientSession() as session:
        client = Rivian(
            request_timeout=30,
            session=session,
            access_token=entry_data["access_token"],
            refresh_token=entry_data["refresh_token"],
            user_session_token=entry_data["user_session_token"],
        )
        try:
            await client.create_csrf_token()
            user_record = await public_operation(
                "GetUserInfo",
                client.get_user_information(include_phones=False),
                root_field="currentUser",
            )
            report["operations"].append(user_record)
            user_response = (
                user_record.get("response", {}).get("data", {}).get("currentUser")
            )
            if not isinstance(user_response, Mapping):
                raise ProbeHalt("Rivian user or vehicle metadata was not returned.")

            user_id = user_response.get("id")
            vehicles = user_response.get("vehicles", [])
            if not isinstance(user_id, str) or not vehicles:
                raise ProbeHalt("No delivered Rivian vehicles were returned.")

            if "account" in query_sets:
                wallboxes = await public_operation(
                    "GetRegisteredWallboxes",
                    client.get_registered_wallboxes(),
                    root_field="getRegisteredWallboxes",
                )
                report["operations"].append(wallboxes)
                await pause()
                wallbox_data = (
                    wallboxes.get("response", {})
                    .get("data", {})
                    .get("getRegisteredWallboxes", [])
                )
                for wallbox in wallbox_data or []:
                    serial_number = wallbox.get("serialNumber")
                    if not isinstance(serial_number, str) or not serial_number:
                        continue
                    report["operations"].append(
                        await graphql_operation(
                            session,
                            client,
                            endpoint=GRAPHQL_CHARGING,
                            operation="WallChargerHistory",
                            query="""
                                query WallChargerHistory(
                                  $serialNumber: String!, $startDate: String,
                                  $endDate: String
                                ) {
                                  getWallChargerHistory(
                                    serialNumber: $serialNumber,
                                    startDate: $startDate,
                                    endDate: $endDate
                                  ) {
                                    transactionId startDateTime endDateTime
                                    totalEnergyKwh rangeAddedKm
                                    vehicleInfo { name vin }
                                  }
                                }
                            """,
                            variables={
                                "serialNumber": serial_number,
                                "startDate": (datetime.now(UTC) - timedelta(days=30))
                                .date()
                                .isoformat(),
                                "endDate": datetime.now(UTC).date().isoformat(),
                            },
                            root_field="getWallChargerHistory",
                        )
                    )
                    await pause()

            for index, user_vehicle in enumerate(vehicles, start=1):
                vehicle_id = user_vehicle.get("id")
                if not isinstance(vehicle_id, str):
                    continue
                vehicle_report: dict[str, Any] = {
                    "alias": f"vehicle_{index}",
                    "metadata": user_vehicle,
                    "operations": [],
                }
                report["vehicles"].append(vehicle_report)

                if "account" in query_sets:
                    vehicle_report["operations"].append(
                        await public_operation(
                            "DriversAndKeys",
                            client.get_drivers_and_keys(vehicle_id),
                            root_field="getVehicle",
                        )
                    )
                    await pause()
                if "vehicle-state" in query_sets:
                    vehicle_report["operations"].extend(
                        await capture_vehicle_state(
                            session,
                            client,
                            vehicle_id,
                            args.subscription_seconds,
                        )
                    )
                    vehicle_report["operations"].append(
                        await targeted_introspection(
                            session, client, GRAPHQL_GATEWAY, "VehicleState"
                        )
                    )
                    await pause()
                if "charging" in query_sets:
                    vehicle_report["operations"].extend(
                        await capture_charging(session, client, vehicle_id)
                    )
                    await pause()
                if {"vehicle-state", "charging"} & query_sets:
                    vehicle_report["operations"].append(
                        await capture_parallax(
                            session,
                            client,
                            vehicle_id,
                            args.subscription_seconds,
                        )
                    )
                    await pause()
                if "history" in query_sets:
                    vehicle_report["operations"].extend(
                        await capture_history(session, client, vehicle_id, user_id)
                    )
                    await pause()
                if "ota-images" in query_sets:
                    vehicle_report["operations"].extend(
                        await capture_ota_images(session, client, vehicle_id)
                    )
                    await pause()
        finally:
            await client.close()

    safe_report = sanitize(report, secrets)
    encoded = json.dumps(safe_report, indent=2, sort_keys=True) + "\n"
    if any(secret in encoded for secret in secrets):
        raise ProbeHalt(
            "A config-entry secret survived sanitization; output was discarded."
        )

    args.output.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output / f"{timestamp}-{args.label}.json"
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output_file:
        output_file.write(encoded)
    return output_path


async def run_correlation(args: argparse.Namespace) -> Path:
    """Run one interactive, read-only Parallax correlation session."""
    sanitizer_self_test()
    if not LABEL_PATTERN.fullmatch(args.label):
        raise ProbeHalt(
            "Label must contain only lowercase letters, digits, hyphens, or underscores."
        )
    if not 1 <= args.settle_seconds <= 15:
        raise ProbeHalt("Settle time must be between 1 and 15 seconds.")

    entry = load_rivian_entry(args.config_entries, args.entry_id)
    entry_data = entry["data"]
    secrets = known_secrets(entry)
    report: dict[str, Any] = {
        "metadata": {
            "captured_at": datetime.now(UTC).isoformat(),
            "label": args.label,
            "command": "correlate",
            "repository_commit": git_commit(),
            "homeassistant_version": version("homeassistant"),
            "rivian_python_client_version": version("rivian-python-client"),
        },
        "vehicle": None,
        "correlation": None,
    }

    async with aiohttp.ClientSession() as session:
        client = Rivian(
            request_timeout=30,
            session=session,
            access_token=entry_data["access_token"],
            refresh_token=entry_data["refresh_token"],
            user_session_token=entry_data["user_session_token"],
        )
        try:
            await client.create_csrf_token()
            user_record = await public_operation(
                "GetUserInfoForCorrelation",
                client.get_user_information(include_phones=False),
                root_field="currentUser",
            )
            user_response = (
                user_record.get("response", {}).get("data", {}).get("currentUser")
            )
            if not isinstance(user_response, Mapping):
                raise ProbeHalt("Rivian user or vehicle metadata was not returned.")
            vehicles = [
                vehicle
                for vehicle in user_response.get("vehicles", [])
                if isinstance(vehicle, Mapping) and isinstance(vehicle.get("id"), str)
            ]
            if args.vehicle_id:
                vehicles = [
                    vehicle
                    for vehicle in vehicles
                    if vehicle.get("id") == args.vehicle_id
                ]
                if not vehicles:
                    raise ProbeHalt("The requested vehicle ID was not returned.")
            elif len(vehicles) > 1:
                vehicle_ids = ", ".join(str(vehicle["id"]) for vehicle in vehicles)
                raise ProbeHalt(
                    "Multiple vehicles were returned; rerun with --vehicle-id using "
                    f"one of: {vehicle_ids}"
                )
            if not vehicles:
                raise ProbeHalt("No delivered Rivian vehicle was returned.")

            vehicle = vehicles[0]
            vehicle_id = str(vehicle["id"])
            report["vehicle"] = vehicle
            report["correlation"] = await interactive_parallax_correlation(
                session,
                client,
                vehicle_id,
                args.settle_seconds,
            )
        finally:
            await client.close()

    safe_report = sanitize(report, secrets)
    encoded = json.dumps(safe_report, indent=2, sort_keys=True) + "\n"
    if any(secret in encoded for secret in secrets):
        raise ProbeHalt(
            "A config-entry secret survived sanitization; output was discarded."
        )

    args.output.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output / f"{timestamp}-{args.label}-correlation.json"
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output_file:
        output_file.write(encoded)
    return output_path


def main() -> int:
    """Run the command-line tool."""
    args = parse_args()
    try:
        if args.command == "self-test":
            sanitizer_self_test()
            print("Sanitizer self-test passed.")
            return 0
        runner = run_correlation if args.command == "correlate" else run_capture
        output_path = asyncio.run(runner(args))
        print(f"Sanitized capture written to {output_path}")
        return 0
    except ProbeHalt as err:
        print(f"Probe stopped: {err}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
