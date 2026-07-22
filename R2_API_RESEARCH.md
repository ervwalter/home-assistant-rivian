# Rivian R2 API Research

## Scope and status

This document is the canonical, sanitized record for R2 development plumbing and
read-only API research. It distinguishes Rivian data availability from client
library gaps, integration omissions, model gating, and values derived by other
applications. Production entity changes and remote-command testing are outside
this phase.

The planned parked-state, sleep, climate, closure, window, lock, gear,
drive-mode, active-drive, active-navigation, and home-AC charging captures are
complete. Rare states that were not naturally available, such as DC fast
charging and OTA installation, remain follow-ups rather than blockers for the
API inventory.

The local Home Assistant instance authenticates, discovers the R2, loads the
integration from the working-tree symlink, and survives a full restart without
re-entering credentials. A narrow research-plumbing guard allows vehicle setup to
continue when the removed legacy charging query fails. No production Home
Assistant instance was changed.

## Test environment

| Item | Observed value |
| --- | --- |
| Base repository commit | `4279cc16903cc3adf571a74fe591296c0cf36825` |
| Research branch | `r2-api-research` |
| Research date | 2026-07-19 through 2026-07-22 |
| Devcontainer Python | 3.13.5 |
| Home Assistant | 2026.2.3 |
| `rivian-python-client` | 2.0.0 |
| Integration version | 0.0.0 development version |
| Production integration version | Not queried; production left untouched |
| API vehicle model | `R2` |
| API model year | 2027 |
| Vehicle software | `2026.24.40` |
| Account role | Dedicated secondary driver |

The devcontainer setup originally inherited an expired, unused Yarn apt source.
Removing that source allowed `libturbojpeg0` and `ffmpeg` to install normally.
`.devcontainer/setup` completes and `config/custom_components` resolves to the
working tree.

The macOS bind mount repeatedly corrupted Home Assistant's SQLite WAL database
during otherwise graceful short research restarts. Home Assistant detected,
renamed, and rebuilt the disposable database, but it recurred. The development
configuration now keeps the recorder at
`sqlite:////tmp/home-assistant-rivian-dev.db` inside the container; account and
integration configuration remains in the persistent ignored `config/` tree.

The initial repository baseline passed `ruff check custom_components/`. Ruff
0.15.22's format check reported four pre-existing files (`button.py`, `lock.py`,
`sensor.py`, and `switch.py`) that differ from its current formatter. This is
baseline drift, not an R2 finding.

## Safety and privacy constraints

- All live research operations are reads or subscriptions. The probe contains no
  GraphQL mutations and calls no vehicle-command method.
- The complete read inventory did not visibly wake an asleep R2. This result does
  not apply to mutations, remote commands, phone-key activity, or other untested
  clients.
- No phone key is paired and no remote control is executed.
- Credentials are entered only through the local Home Assistant config flow.
- Captures may retain vehicle identifiers, VINs, names, locations, and public-key
  shapes because their formats and relationships are useful research evidence.
- Passwords, OTPs, access/refresh/session/CSRF tokens, cookies, authorization
  headers, private keys, and signed-URL credentials are redacted before output.
- Captures are owner-readable only and remain under ignored
  `config/rivian-research/`.
- The probe pauses between one-shot requests and stops on authentication,
  rate-limit, or account/session-lock responses.

One sanitizer self-test specifically covers nested headers, known config-entry
secrets, and AWS-signed OTA URLs. A failed self-test prevents capture output.

## Integration and API data flow

### Current integration

1. The config flow exchanges email, password, and optional OTP for access,
   refresh, and user-session tokens. Home Assistant stores the email and tokens
   in ignored config-entry storage; it does not retain the password.
2. `UserCoordinator` polls `getUserInfo` every 30 seconds for vehicle metadata,
   supported features, enrollment metadata, and account information.
3. `VehicleCoordinator` opens the legacy JSON `vehicleState` GraphQL
   subscription and requests 116 fields.
4. `ChargingCoordinator` polls `getLiveSessionData` for eight fields every 30
   seconds while plugged in and every 15 minutes while unplugged.
5. Drivers/keys, wallboxes, OTA details, and images use separate read queries.

### R2-era split

The R2 account advertises `PX_STATE_ALL`, `CHARG_DATA_PX`,
`VEHICLE_CONNECTIVITY_PARALLAX`, and all observed `PVS_*_ST8` state-domain
features. The official app's richer R2 state is delivered primarily through the
`ParallaxMessages` GraphQL WebSocket subscription. Each message identifies an
RVM topic and carries a base64-encoded protobuf payload.

The legacy `vehicleState` subscription still exists and accepts all fields known
to client 2.0.0, but most R2 values are null or stale there. The legacy
`getLiveSessionData` query no longer exists at all.

Public protocol evidence used to interpret the live captures is unofficial and
APK-derived because Rivian does not publish a consumer API specification:

- [Parallax subscription](https://github.com/kaedenbrinkman/rivian-api/blob/0ad0b2c12186bd8272863dd801c899e30c7f520d/app/parallax/subscription.md)
- [Parallax payload decoding](https://github.com/kaedenbrinkman/rivian-api/blob/0ad0b2c12186bd8272863dd801c899e30c7f520d/app/parallax/decoding.md)
- [Charging RVMs](https://github.com/kaedenbrinkman/rivian-api/blob/0ad0b2c12186bd8272863dd801c899e30c7f520d/app/parallax/domains/charging.md)
- [Energy and charging graph RVMs](https://github.com/kaedenbrinkman/rivian-api/blob/0ad0b2c12186bd8272863dd801c899e30c7f520d/app/parallax/domains/energy.md)
- [Vehicle feature flags](https://github.com/kaedenbrinkman/rivian-api/blob/0ad0b2c12186bd8272863dd801c899e30c7f520d/app/feature-flags/vehicle.md)

## Endpoint and operation inventory

| Surface | Operation | Live result | Interpretation |
| --- | --- | --- | --- |
| Gateway HTTP | `getUserInfo` | 200 | R2 metadata and 57 available features |
| Gateway WS | `vehicleState` | 101 + frames | All 135 client-known fields schema-accepted |
| Gateway WS | `parallaxMessages` | 101 + 42 topics | Principal current R2 state transport |
| Charging HTTP | `getLiveSessionData` | 400 validation | Root field removed from schema |
| Gateway WS | `chargingSession` | 101 + object | Current scalar shape; all live values null at charge-complete state |
| Charging HTTP | `getLiveSessionHistory` | 200 | Current metadata/history shape; not live telemetry replacement |
| Charging HTTP | `getCompletedSessionSummaries` | 200, empty | Supported; no sessions visible to secondary driver |
| Charging HTTP | `getSessionHistory` | 400 validation | Removed from schema |
| Charging HTTP | `getSessionStatus` probe | 400 validation | Requires `jobId`; public-charge job status, not vehicle state |
| Charging HTTP | `getRegisteredWallboxes` | 200, empty | No applicable wallbox for this account |
| Gateway HTTP | current OTA detail | 200 | Works when queried separately |
| Gateway HTTP | available OTA detail | 200 + `NOT_FOUND` | Expected when no update is available |
| Gateway HTTP | client combined OTA query | API exception | Available-detail error masks current detail |
| Gateway HTTP | vehicle images | 200, 18 images | R2 image and overlay inventory works |
| Gateway HTTP | mobile configuration | 200 | R2 trim, drive, exterior, and interior metadata |
| Gateway HTTP | charging schedules | 200 | Current schedule shape works |
| Gateway HTTP | estimated range | 200 | Returns direct estimate for requested starting SOC |
| Gateway HTTP | settings / connected products | 400 validation | Packaged schema is stale for these selections |
| Introspection | targeted `__type` probes | Rejected | Apollo introspection disabled or generic validation failure |

The server's suggestion that removed charging queries might mean
`getSessionStatus` is misleading. Validation shows that operation requires a
public-charging `jobId` and is unrelated to live vehicle telemetry.

## Existing Home Assistant entity inventory

### Exact R1-only model gating

Sensor and binary-sensor descriptions are selected with substring checks such
as `if model in vehicle["model"]`. The configured groups are:

| Platform | Model key | Descriptions |
| --- | --- | ---: |
| Sensor | `R1` | 67 |
| Sensor | `R1S` | 1 |
| Binary sensor | `R1` | 41 |
| Binary sensor | `R1T` | 6 |
| Binary sensor | `R1S` | 4 |

The exact API model `R2` matches none of the 108 common `R1` sensor and
binary-sensor descriptions. Variant descriptions raise the static total to 119,
but none applies to `R2`. The options-flow device selector separately accepts
only exact `R1S` and `R1T` model values for vehicle controls.

### Observed R2 device

The unmodified entity baseline contains 27 entities:

| Domain/source | Count | Observed state |
| --- | ---: | --- |
| Vehicle images | 16 | Available |
| Charging sensors | 7 | All unavailable |
| Drivers | 1 | Available, one driver |
| Keys | 1 | Available, zero visible keys for secondary driver |
| Device tracker | 1 | Available |
| Software update | 1 | Available, no update pending |

No sensor or binary-sensor entity from the `R1` groups is created. The exact
seven charging entities are charging cost, energy delivered, range added, rate,
power (named “Charging Speed”), start time, and elapsed time.

A fresh HA restart during confirmed active 10.7 kW AC charging produced the same
27 entities and all seven charging sensors remained `unavailable`. This matches
the coordinator-level validation failure rather than vehicle inactivity.

## Legacy vehicle-state field matrix

The integration's 116 requested fields are a strict subset of the client's 135
known fields. A successful isolated subscription established that all 135 are
accepted by the live schema: none was rejected and none was omitted from the
initial object. In the captured charging-complete state, 13 were present with
non-null data and 122 were present with null data.

| Field | Source | Observed value class | Freshness at capture | Units/enum | Confidence |
| --- | --- | --- | --- | --- | --- |
| `activeDriverName` | Legacy WS | Present | Prior day | String | High |
| `batteryCapacity` | Legacy WS | Present | ~20 hours old | kWh | High |
| `batteryLevel` | Legacy WS | Present | ~5 minutes old | Percent | High |
| `batteryLimit` | Legacy WS | Present | Prior day | Percent | High |
| `chargerState` | Legacy WS | `charging_complete` | ~5 minutes old | String enum | High |
| `distanceToEmpty` | Legacy WS | Present | ~2 hours old | km | High |
| `driveMode` | Legacy WS | `everyday` | Prior day | String enum | High |
| `geoLocation` | Legacy WS | `HOME` class | Current | Place enum/string | High |
| `gnssLocation` | Legacy WS | Structured location | Current | degrees + timestamp | High |
| `otaCurrentVersion` | Legacy WS | Present | Prior day | Version string | High |
| `serviceMode` | Legacy WS | `off` | Prior day | String enum | High |
| `timeToEndOfCharge` | Legacy WS | 0 | ~5 minutes old | seconds | Medium |
| `vehicleMileage` | Legacy WS | Present | Prior day | fixed-point metres | High |
| Remaining 122 fields | Legacy WS | Present with null | Initial snapshot | Field-specific | High for null, low for unsupported inference |

This mixed timestamp set proves that “present” does not imply “fresh.” A bounded
capture also showed many sparse frames after the initial response. Captures merge
non-null updates and retain every raw frame so stale and current values can be
distinguished later.

The 19 client-known fields not requested by the integration are:

- `batteryCellType`, `batteryNeedsLfpCalibration`,
  `btmOcHardwareFailureStatus`, `cloudConnection`, `geoLocation`, `gnssError`,
  and `rearHitchStatus`.
- `chargingDisabledACFaultState`, `chargingDisabledAll`,
  `chargingTimeEstimationValidity`, `chargingTripTargetMinsRemaining`,
  `chargingTripTargetSoc`, and `coldRangeNotification`.
- `closureChargePortDoorNextAction`, `closureFrunkNextAction`,
  `closureSideBinLeftNextAction`, `closureSideBinRightNextAction`,
  `closureTailgateNextAction`, and `closureTonneauNextAction`.

The packaged schema additionally names `chargingDisabledAC` and
`supportedFeatures`, but the client constant does not request them in the
subscription. Supported features are already returned by `getUserInfo`.

## Parallax field and domain matrix

A 30-second charging-complete capture requested 43 focused RVMs and received 42
initial payloads. Only `body.windows.states` emitted no frame in that window.
This means “not observed in window,” not schema rejection: Parallax does not
guarantee an initial frame for every requested topic.

| Domain/value | RVM source | Charging-complete observation | Units/shape | Freshness | Confidence |
| --- | --- | --- | --- | --- | --- |
| SOC and pack capacity | `energy.high_voltage.battery_state` | 84.7%, 91.52 kWh | Double | Current | High |
| Battery cell temperature | same | avg 31.4, max 31.6, min 29.0 | °C float | Current | High |
| Battery cold state | same | Enum value 11 | Protobuf enum | Current | Medium; label unknown |
| Tire pressures | `dynamics.tires.state` | Position 3 at 2.83 bar matched legacy rear-left; position 4 at 2.90 matched rear-right; positions 1/2 were equal at 2.925 | bar double | Concurrent | High for rear positions and all values; medium for front labels |
| Cabin temperature | `comfort.cabin.cabin_temperatures` | One populated temperature | °C float | Current | Medium; sensor position unconfirmed |
| Range | `dynamics.vehicle.range` | Populated | km integer | Current | High |
| Odometer | `dynamics.vehicle.odometer` | Populated | km integer | Prior day | High by legacy correlation |
| GNSS | `dynamics.vehicle.gnss` | Location, altitude, speed, signed heading, and GPS-epoch timestamp | degrees/metres/m/s/GPS ms | Current; about 60-second cadence while driving | High from repeated active-drive frames |
| Locks | `body.locks.states` | Positions 1, 2, 3, 4, 5, and 7 changed together; 1 locked, 2 unlocked | Nested enums | Current | High for state; positions align with correlated closures but cannot be locked independently |
| Closures and windows | `body.closures.states` | Four doors, frunk, liftgate, and five windows correlated; 1 open, 2 closed | Nested enums | Current | High for paired transitions; medium-high for baseline-open/close-only cases |
| Dedicated windows topic | `body.windows.states` | No frame in any of nine physical-correlation sessions | Unknown | Not observed | High that this R2 reports its five windows through `body.closures.states`; not proof the topic is globally unsupported |
| Plug/session status | `charging.session.status` | Unplugged `(1, 1)`, active `(2, 3)`, complete `(2, 4)`, scheduled `(2, 5)`, user-stopped `(2, 8)` | Plug/display/EVSE enums | Current | High from concurrent legacy and physical transitions |
| Charge target | `charging.session.soc_slider` | 85 | Percent integer | Prior day | High |
| Trip target | `charging.session.trip_target` | Sentinel 65535 in field 2 | Integer | Prior day | Medium |
| Charge breakdown | `energy_edge_compute.graphs.charge_session_breakdown` | Default fields omitted; free-session flag set | kWh, min, km, kW, enum | End of session | High shape, requires active sample |
| Charge graph | `energy_edge_compute.graphs.charging_graph_global` | 14 completed bars at 85% | SOC, kW, epoch ms, enums | Current | High shape |
| Parked energy | `energy_edge_compute.graphs.parked_energy_distributions` | 24h, 8h, and last-park records | kWh/range/minutes | Current | High shape, derived on vehicle |
| Climate status/settings | `comfort.cabin.*` | Off payload empty; active status/type `(2, 1)` | floats/enums/timestamps | Current | High from manual off/on transition |
| Drive mode/gear | `dynamics.vehicle.*` | P/R/N/D and all seven displayed drive modes correlated | Protobuf enums | Current | High for direct observed labels; one supervised cycle per state |
| OTA state/config | `ota.*` | Three topic families populated | Nested messages | Mixed | High availability |
| Wheels | `vehicle.wheels.vehicle_wheels` | Populated | Nested configuration | Prior day | High availability |
| Trip/navigation | `navigation.*` | Active destination, route metadata, and repeated progress | Nested direct state | Current; progress every five seconds | High for availability and progress fields; nested trip-info labels partly unresolved |
| Network/power | `vehicle.network.state`, `vehicle.power.state` | Populated | Nested enums | Current | High availability |

Proto3 omits fields at their default values. Therefore a four-byte completed
charge breakdown containing only empty cost and a free-session flag does not mean
that the other fields were rejected. They need an active-session capture.

The 91.52 kWh battery value remained fixed while state of charge increased and
matched the legacy `batteryCapacity` value. It is pack capacity, not current
stored energy. A production entity must therefore use the existing Battery
Capacity semantic; current stored energy would require a clearly labeled
derivation and is not supplied directly by this observed field.

The nested GNSS timestamp is milliseconds in the GPS epoch rather than the Unix
epoch. For the captured software, conversion to Unix milliseconds is
`gps_ms + 315964800000 - 18000`, where the final term is the current GPS-to-UTC
leap-second offset. Treating the raw value as Unix time places current samples in
2016 and can make newer legacy GNSS data accidentally mask the error.

The tire correlation separates direct evidence from external corroboration.
Concurrent R2 and legacy captures directly establish position 3 as rear-left and
position 4 as rear-right because their distinct 2.83 and 2.90 bar values matched
exactly. Positions 1 and 2 were both 2.925 bar, so the live vehicle proves only
that they are the two front tires, not which is left or right. Rivolt documents
the complete ordering as 1=front-left, 2=front-right, 3=rear-left, and
4=rear-right in its
[tire decoder](https://github.com/apohor/rivolt/blob/9d0b3362f4f9a0da6d625009a9dd74c6c64e8a28/internal/rivian/parallax.go#L846-L887).
The front labels remain externally corroborated rather than independently live
confirmed until natural pressure drift produces distinguishable concurrent
values.

Observed charging enum integers do not match all older public assumptions: the
completed charging graph used state value 4, while public captures have observed
value 3 during active charging. Enum labels must be correlated from live
transitions instead of hard-coded from an unverified schema.

Concurrent captures established the status combinations used for the initial R2
implementation. Legacy `chargerState` corroborated scheduled, active, complete,
and user-stopped transitions, but is not authoritative for physical connection:
after unplugging it reported `charging_ready` while Parallax `(1, 1)` correctly
reported unplugged. The legacy feed supplies string semantics and supporting
timestamps; its values are not numerically interchangeable with Parallax enums.

The supervised physical-correlation sessions established the following direct
R2 position map. Every listed physical action changed only the identified
position's `state` within `body.closures.states`, apart from unrelated metadata
changes when gear selection triggered automatic locking. State 1 means open and
state 2 means closed.

| Position | Correlated physical opening | Evidence |
| --- | --- | --- |
| 1 | Driver door | Open observation plus repeated all-closed baselines |
| 2 | Front-passenger door | Open baseline followed by isolated close |
| 3 | Rear-driver door | Isolated open and close pair |
| 4 | Rear-passenger door | Isolated open and close pair |
| 5 | Frunk | Open baseline followed by isolated close |
| 7 | Liftgate | Isolated open and close pair |
| 12 | Driver window | Isolated down and up pair |
| 13 | Front-passenger window | Isolated down and up pair |
| 14 | Rear-driver window | Isolated down and up pair |
| 15 | Rear-passenger window | Isolated down and up pair |
| 16 | Liftgate glass | Open baseline followed by isolated close |

The R2 therefore exposes all five windows, including the opening liftgate
glass, as closure positions 12 through 16. `body.windows.states` emitted no
frame in the initial field inventory or any of the nine physical-correlation
sessions, including the sessions in which windows were moved. This is strong
vehicle-specific evidence that the useful window state is the closures topic;
it is not a claim that the dedicated topic is unsupported on every model or
software version.

`body.locks.states` contains positions 1, 2, 3, 4, 5, and 7, matching the six
non-window closure positions. All six report state 1 when locked and state 2
when unlocked. The aggregate state changed consistently across user-confirmed
locked/unlocked baselines and the vehicle's automatic lock/unlock sequence when
selecting Reverse and returning to Park. Individual lock-position names follow
the directly correlated closure positions, but the six locks cannot be
actuated independently, so their individual identity is structurally aligned
rather than independently transitioned. The legacy `vehicleState` stream
emitted no corresponding door, window, or closure value during the earlier
manual transition.

Independent protocol source in
[Rivolt's Parallax decoder](https://github.com/apohor/rivolt/blob/9d0b3362f4f9a0da6d625009a9dd74c6c64e8a28/internal/rivian/parallax.go)
corroborates closure states 3/4/5 as ajar/opening/closing, all of which mean not
closed for an HA binary sensor; lock state 3 is partially unlocked. It also
identifies cabin preconditioning phases 1 through 4 as running and state 8 as
unavailable. These labels were not all naturally produced during the supervised
R2 session, so production retains the raw enum integers in diagnostics and does
not generalize unrelated model-specific enums.

The stationary, brake-held gear sequence directly mapped
`dynamics.vehicle.gear` as follows:

| Enum | Displayed gear |
| --- | --- |
| 1 | Park |
| 2 | Reverse |
| 3 | Neutral |
| 4 | Drive |

Reverse and the final return to Park also changed lock state because of normal
vehicle behavior. Neutral and Drive changed only the gear RVM, and the enum
progression plus final return established the labels independently of the lock
side effect.

All seven drive modes offered by this R2 were selected while stationary and
changed only `dynamics.vehicle.drive_mode`:

| Enum | Displayed drive mode |
| --- | --- |
| 2 | All-Purpose |
| 4 | Rally |
| 8 | Sport |
| 9 | Conserve |
| 11 | All-Terrain |
| 12 | Soft Sand |
| 15 | Snow |

All-Purpose was observed both as a baseline and as the final return state. Snow
was observed as the final state of one session and the baseline of the next.
The remaining modes each have one direct labeled transition.

The charge-port-door session began with the flap visibly open and then closed
it. None of the requested RVMs changed and no frame was emitted during the
settle window; the open baseline also contained no distinct position in
`body.closures.states`. The charge-port door therefore remains unobserved, not
mapped to plug state or inferred from another closure.

Physical actions were intentionally limited to one supervised cycle per state
because repeating the complete matrix was burdensome. Clean open/close pairs
were captured where listed above, while several items used an explicitly
verified open baseline followed by close. This is sufficient direct evidence
for the map, but it is weaker than the probe's original two-cycle acceptance
gate. Unknown future positions and enum integers must remain unassigned rather
than being guessed from sequence.

The manual climate transition changed `cabin_preconditioning_status` from an
empty/default payload to status 2/type 1. During the active capture, cabin
temperature field 3 updated every 1–7 seconds and fell from approximately 28.2°C
to 27.8°C. The legacy `vehicleState` subscription emitted none of its climate,
preconditioning, defrost, seat, steering-wheel, or pet-mode fields during the
same interval.

A later 30-second capture while the R2 was already being driven established the
additional comfort payload shapes without sending commands. HVAC settings field
1 was the cabin target temperature (23.5°C while driving, compared with 22.0°C
in parked captures). Cabin temperatures continued to update field 3. The
preconditioning status was code 8 while ordinary driving HVAC was available in
the vehicle; this code means remote preconditioning is unavailable, not that
the cabin-temperature feed or normal driving HVAC has failed. Production can
therefore report remote preconditioning off for code 8 only while power is `go`
or the selected gear is Reverse, Neutral, or Drive; code 8 remains unavailable
without that independent in-use evidence.

The same capture observed Pet Mode state field 1 as code 2, ventilation setting
field 1 as code 1, climate-hold duration field 1 as 7,200 seconds, a nested
climate-hold end timestamp in field 4, defrost/defog field 1 as code 4, and seven
seat-conditioning records containing component and conditioning-type codes.
Public decoder evidence maps Pet Mode codes 0/1/2/3 to off/on/disabled/faulty.
Seat positions, conditioning types, levels, defrost states, and climate-hold
enums were not independently transitioned, so those integers remain raw
diagnostic/research evidence rather than production entity labels.

The unplugged transition produced plug/display values `(1, 1)` with no EVSE
field, compared with `(2, 4, 1)` at charging complete. The same capture reported
`vehicle.power.state = 3`, which independent live-capture evidence maps to
`ready`: the closed and locked vehicle was still awake immediately after being
unplugged.

Two captures attempted shortly after the R2 was left closed, locked, and
unplugged still received fresh telemetry and reported `vehicle.power.state = 3`.
After a longer settled period, the isolated five-second
`asleep-correlation-minimal` capture reported power state 1 in its first
Parallax frame. The official app continued to show the R2 asleep after the probe
closed, establishing that power state 1 means asleep and that this bounded
read-only subscription did not visibly wake the vehicle. A subsequent full
default capture exercised the account, legacy vehicle-state, charging, history,
OTA/image, typed charging, and Parallax read surfaces for approximately one
minute. The app still showed the R2 asleep afterward. That capture replayed the
same power-state-1 frame and contained no vehicle timestamp newer than the cached
sleep snapshot, so none of the tested reads produced an observed wake or fresh
awake telemetry. Power state 3 means ready/awake; the earlier attempts had not
yet reached API sleep.

## Active-drive and navigation telemetry

Two supervised, read-only captures were taken while the R2 was already being
driven with an active navigation route. No vehicle command was sent and the
driver did not interact with the probe. The captures are
`active-drive-navigation` (45-second subscription window) and
`active-drive-navigation-long` (90-second subscription window).

`dynamics.vehicle.gnss` has the following directly correlated wire shape on
this R2:

| Field | Wire type | Direct active-drive meaning |
| --- | --- | --- |
| 1 | double | Latitude, degrees |
| 2 | double | Longitude, degrees |
| 3 | double | Altitude, metres |
| 4 | float | Speed, metres per second |
| 5 | float | Heading/bearing, signed degrees |
| 6-9 | float | GNSS quality/accuracy values; labels unresolved |
| 10 | varint | GPS-epoch timestamp, milliseconds |

Three consecutive GNSS frames reported 29.29, 28.60, and 28.33 m/s while the
heading changed from 89.74 through 85.18 to 72.85 degrees. They arrived almost
exactly 60 seconds apart. Parked R2 captures omit field 4 at its protobuf
default and retain field 5, sometimes as a negative signed angle; consumers
should normalize heading for display without changing the retained raw value.

The same drive directly establishes `vehicle.power.state = 4` as the active
driving/Go state on R2: it was concurrent with `dynamics.vehicle.gear = 4`
(Drive), moving GNSS frames, advancing odometer, and decreasing route distance.
Power states now directly observed on R2 are 1 asleep, 3 ready/awake, and 4 Go.

With a route active, `navigation.navigation_service.trip_progress` emitted every
five seconds. Top-level field 4 is remaining route distance in metres and field
5 is remaining drive time in seconds. Over one 90-second window they decreased
from 20,500 to 17,965 metres and from 896 to 808 seconds. Field 6 is a nested
live-motion record containing coordinates, speed, heading, and timestamp; its
speed and heading agreed with the ordinary GNSS topic. This gives active
navigation a roughly five-second motion source, compared with the route-
independent GNSS topic's roughly 60-second cadence.

`navigation.navigation_service.trip_info` contained the destination name and
address, destination coordinates, route legs, route polyline, ETA information,
route preferences, and optional charging-stop structures. It is direct,
state-dependent navigation data and must be cleared when the route ends rather
than retained as stale state. The payload is large and its full nested field
labels remain a separate decoding task; the destination and route data were
confirmed without intercepting mobile traffic.

No verified Parallax topic or field supplied instantaneous traction power,
acceleration, or direct rolling efficiency. `energy.high_voltage.battery_state`
reported SOC in 0.1-percent steps and usable pack capacity, while odometer
reported whole kilometres. Those values can support a coarse completed-trip
estimate (`distance / (SOC delta * usable capacity)`), but their quantization is
too large for a trustworthy one- or two-minute mi/kWh entity. Rivolt likewise
derives drive energy from SOC delta and pack capacity rather than a distinct
Rivian efficiency field. Average/max speed, acceleration, trip distance, and
trip efficiency can be derived from repeated direct samples, but must be labeled
as derived rather than Rivian-supplied.

## Charging analysis

### Why every existing charging entity is unavailable

Both the integration's eight-field request and the client's complete 17-field
request fail before response data is evaluated:

```text
Cannot query field "getLiveSessionData" on type "Query".
```

This is a removed root operation, not an R2 response containing null fields. The
charging coordinator never has a successful initial result, so every entity that
depends on it is unavailable.

| HA entity | Legacy dependency | Evidence-backed R2 explanation |
| --- | --- | --- |
| Charging cost | `currentPrice` + `currentCurrency` | Query removed; no response object |
| Energy delivered | `totalChargedEnergy` | Query removed; Parallax supplied 0.1 kWh after one minute |
| Range added | `rangeAddedThisSession` | Query removed; Parallax field is direct but proto-default until nonzero |
| Charging rate | `kilometersChargedPerHour` | Query removed; Parallax supplied 59–60 km/h |
| Charging power | `power` | Query removed; Parallax supplied 10.7–10.9 kW |
| Charging start time | `startTime` | Query removed; absent from breakdown and must come from graph/session metadata or derivation |
| Charging elapsed time | `timeElapsed` | Query removed; Parallax supplied one elapsed minute |

The integration's fields are not merely “undefined outside a session”; the
operation fails during GraphQL validation in every state.

### Current alternatives

The typed gateway `chargingSession(vehicleId)` subscription is schema-valid and
uses direct scalars rather than `{value, updatedAt}` records. At charging complete
it returned an empty chart and explicit null for all 11 selected live fields.
It remained entirely null during confirmed active 10.7 kW home AC charging.
Public app-derived evidence says that it is used on a charging detail view for
some charger types, but home AC/L1/L2 data is carried by Parallax on this R2.

The R2 advertises `CHARG_DATA_PX`. The modern charge-session breakdown directly
defines total/pack/thermal/outlet/system kWh, elapsed and remaining minutes, range
added, current power, range-per-hour, cost, free-session status, and charging
state. `charging_graph_global` supplies SOC/power time buckets and status enums.
This is evidence about availability only; the production integration design is a
future phase.

The early active-AC capture supplied charging state 3, 10.7–10.9 kW, 59–60 km/h,
91–92 minutes remaining, one minute elapsed, and the first 0.1 kWh of total and
pack energy. The graph supplied an active 85%/10.2 kW time bucket. The separate
`charging.session.time_estimation` payload's field 2 moved from 92 to 91 in the
same interval, so older documentation describing only field 1 is incomplete.

At 10–11 minutes, Parallax reported 1.7–1.8 kWh total, 1.6–1.7 kWh to
the battery pack, 0.1 kWh to vehicle systems, zero/default thermal and outlet
energy, 7–8 km added, 82–83 minutes remaining, and 10.7 kW. User-provided
official-app screenshots taken at seven minutes showed 10.7 kW and 1.3 kWh total
split into 1.2 kWh pack, 0.1 kWh systems, and 0.0 kWh thermal/outlets. The API
values and app presentation are the same breakdown with normal sampling and
rounding differences.

Immediately after the user stopped charging but left the cable connected,
legacy `chargerState` changed to `charging_user_stopped` and
`timeToEndOfCharge` became zero. Parallax plug status remained 2, display status
changed to 8, power and remaining time became proto-default/omitted, and the
final breakdown remained available: 2.2 kWh total, 2.1 kWh pack, 0.1 kWh
systems, 10 km added, and 13 minutes. The old 60 km/h rate and charging-state
enum 3 also persisted while power was absent, so neither value alone is a safe
active-charging test. New graph buckets used state 8 with zero/default power.

Immediately after unplugging, plug/display returned to `(1, 1)`, EVSE type was
omitted, the graph became empty, and the breakdown cleared all final totals and
duration/range fields. Live totals therefore cannot be recovered after unplug
from this stream. Legacy `chargerState` became `charging_ready` even though the
cable was physically disconnected, making the legacy enum unreliable as a plug
sensor on this R2. The typed charging subscription remained all-null throughout.

## Charging history

- `getCompletedSessionSummaries` is current and supports summary metadata,
  including transaction grouping and data sources. It returned an empty list to
  the dedicated secondary driver both before and immediately after a newly
  observed home AC session, so a detailed-by-transaction query could not be
  attempted safely. This is evidence of role visibility or ingestion delay, not
  an absence of charging activity.
- `getLiveSessionHistory` exists. Current chart points use `{time, kw}`, not the
  packaged client's stale `{value, updatedAt}` shape. At charging complete its
  chart and metadata were empty/null.
- `getSessionHistory` is removed.
- Wallbox history was not applicable because the account returned no registered
  wallbox.

Owner-account authorization and a completed transaction identifier are explicit
follow-ups for historical-detail coverage.

## OTA, images, configuration, and schedules

The client combines current and available OTA detail in one query. When no
available update exists, `OTA_VERSION_NOT_FOUND` causes the client call to fail
and hides otherwise valid current release details. Split probes establish that
current version and release-note metadata work, while the available detail is
null with an expected not-found error.

The image endpoint returned 18 R2 image records, including front, in-use,
overhead, rear, side-charging, side-rear, side, and three-quarter placements plus
charging/trailer/car-wash overlays. The live endpoint returns arrays even though
parts of the packaged SDL describe singular values.

Mobile configuration, a seven-day charging schedule, and estimated range at a
requested starting SOC all work. Packaged `settings` and `connectedProducts`
selections were rejected, showing more stale SDL surface area.

## Supported features and controls

`getUserInfo` returned 57 features with status `AVAILABLE`:

```text
ACTIVE_TRIP, ACTV_USR, AUTONOMY_PLUS, CAR_WASH_MODE, CHARG_DATA_PX,
CHARG_NTW_EA, CHARG_NTW_IONNA, CHARG_CLEAN_NRG, SMART_CHARG,
CHARG_TRIP_TARGET, CONNECT_PLUS, CONN_SUB, ENRG_CLD_WTHR,
ENRG_MONTR_ACTIVE, ENRG_MONTR_PARK, VIDEO_DOWNLOADING, LIVE_CAM, V_GGVS,
HMAC_TIMEOUT_90S, KEY_FOB_2, KEY_PAAK, LIFTGATE_CMD, V_SRCH_PLUS, V_SATMAP,
MOBILE_WHEEL_SWAP, MOTION_CAM, ORPHANED_PHONE_KEY_RECOVERY_HANDLING,
PVS_BD_CMD, PVS_BD_ST8, PVS_COMF_CMD, PVS_COMF_ST8, PVS_DYN_ST8,
PVS_ENRG_CMD, PVS_ENRG_ST8, PVS_MOD_ST8, PVS_OTA_CMD, PVS_OTA_ST8,
PVS_SEC_CMD, PVS_SEC_ST8, PVS_VEH_ST8, PX_STATE_ALL,
PASSIVE_ENTRY_PROTO_V2, PET_MODE_LOW_TEMP, PIN_KEY_DRIVE, PIN_PROFILE,
PREMIUM_SPEAKER, PRIV_PREF, RVA_MEM, SAVED_LOCATIONS, SCHED_OTA,
SD_CHARG_ENDS_AT, TESLA_NACS, TRAILER_STATUS, TRIP_ADD_STOP, TRIP_NAV_PX,
TRIP_PLANNER_TRAILERS, VEHICLE_CONNECTIVITY_PARALLAX
```

These declarations inventory potential capability; they do not prove that the
existing client's authentication, phone-key pairing, command encoding, or Home
Assistant controls work on R2. No command was sent. Of the integration's older
model-specific closure gates, only `LIFTGATE_CMD` appears in the R2 list; the R2
instead advertises Parallax command-domain features such as `PVS_BD_CMD`,
`PVS_COMF_CMD`, `PVS_ENRG_CMD`, `PVS_OTA_CMD`, and `PVS_SEC_CMD`.

## Official app and Rivian Roamer comparison

The official app is used only as a visible capability checklist. No mobile
traffic was intercepted. The user directly observes substantially more R2 data
in the official app than Home Assistant exposes, including battery/range,
closures, climate, charging, tires/wheels, software, location, and controls. The
live Parallax inventory explains that gap: corresponding R2 topics exist but are
not consumed by this integration.

[Rivian Roamer's telemetry methodology](https://rivianroamer.com/help) says it
stores repeated real-time snapshots and derives drive and charging sessions.
Consequently, a Roamer history value does not prove that Rivian exposes a
separate history field. [Roamer's R2 changelog](https://rivianroamer.com/changelog)
is used as a coverage checklist and notes that some R2 values remain unavailable.

| Capability | Official app | Roamer | API evidence | Integration |
| --- | --- | --- | --- | --- |
| Battery SOC/range/capacity | Directly visible | Direct + stored | Legacy and Parallax direct | R1-gated |
| Battery temperature | Visible | R2 support reported | Parallax direct | Not exposed |
| Live charging power/rate | Visible | Direct snapshot | Parallax direct during session | Legacy query broken |
| Charge sessions | Visible | Derived/stored + summary data | Summary API + live Parallax | Not exposed |
| Drive sessions/routes | Visible | Derived/stored | Navigation/trip topics direct; session derivation external | Not exposed |
| Tires/wheels | Visible | R2 support reported | Parallax direct | R1-gated |
| Closures/locks/climate | Visible | Snapshot data | Parallax direct | R1-gated or absent |
| Software | Visible | Direct + stored | OTA HTTP and Parallax direct | Update entity only |
| Location/map | Visible | Direct + stored | Legacy and Parallax direct | Tracker only |
| Parked/active energy pages | Visible | Derived/stored | Vehicle edge-compute graph topics | Not exposed |

Values described as “stored,” “session,” or “classification” by Roamer are
treated as calculated or inferred unless a matching Rivian field was directly
observed.

## Reproducible capture tool

Run a full capture after stopping the development Home Assistant process so its
subscription does not compete for the account's single WebSocket:

```bash
devcontainer exec --workspace-folder . \
  python3 scripts/rivian_api_probe.py capture \
  --label parked-asleep-unplugged \
  --output config/rivian-research
```

Optional `--query-set` values are `account`, `vehicle-state`, `charging`,
`history`, and `ota-images`; repeat the option to combine sets. `all` is the
default. `--subscription-seconds` controls the bounded observation window from 5
to 120 seconds. If more than one Rivian config entry exists, `--entry-id` is
required.

The tool records raw subscription frames, merged field classifications, a
per-field integration-request flag, timestamps/age and current-versus-stale
classification, decoded known Parallax payloads, generic protobuf wire fields for
unknown messages, and a matching snapshot of registered local Home Assistant
entities and their latest recorder states. It does not accept credentials or
tokens on the command line.

Rivian currently appears to allow only one active subscription WebSocket for a
session. A second socket opened while Home Assistant is connected can acknowledge
but emit no frames. Sequential subscriptions on one isolated probe connection
work, and a single future long-running client should multiplex subscriptions.

### Interactive RVM correlation

The research probe's interactive `correlate` command was used for the completed
closure, window, lock, gear, and drive-mode work. It opens exactly one read-only
`parallaxMessages` WebSocket and keeps that socket open across manually performed
transitions. It does not call a vehicle-command method or send a GraphQL
mutation.

Run it only from an interactive terminal after stopping the development Home
Assistant process:

```bash
devcontainer exec --workspace-folder . \
  python3 scripts/rivian_api_probe.py correlate \
  --label r2-body-gear-correlation \
  --output config/rivian-research
```

If the account contains multiple delivered vehicles, add `--vehicle-id` with the
ID reported by the probe. Authentication values still come exclusively from the
ignored Home Assistant config entry; the command does not accept credentials or
tokens. `--settle-seconds` may be set from 1 to 15 seconds and defaults to four.

The development Home Assistant process was stopped so it did not compete for the
account WebSocket. A second person operated the probe while the vehicle remained
stationary and a driver held the brake for the gear and drive-mode captures. The
sessions covered all four doors, frunk, liftgate, all five opening windows,
charge-port door, lock/unlock, P/R/N/D with a final return to Park, and every
drive mode displayed by this R2.

Each capture retains the raw base64 payload, generic protobuf wire fields,
numeric `position`/`state` or `enum_value` evidence where the shape is known, and
the complete ordered frame stream. Every transition contains its user-supplied
candidate label, frame indexes, changed RVMs, and structural before/after deltas.
The label is deliberately recorded as `candidate_unverified`; the probe never
turns the label into a semantic enum mapping.

The evidence checks applied to the captures were:

- The action must be isolated: no other closure, lock, window, gear, or drive
  mode changes during its capture window.
- A clean inverse transition should restore the earlier numeric state where the
  physical action permits one without restarting the session.
- A closure position is not named from list ordering, one initial snapshot, or
  an older R1 schema. It is named from the direct R2 transition.
- P/R/N/D and drive-mode enums require direct observation of every selected
  vehicle UI label; sequence alone is insufficient evidence.
- Simultaneous lock and door changes are marked confounded unless a separate
  confirmed lock state or automatic lock-only transition identifies the lock
  delta.
- No frame means only “not observed in this session.” An unchanged payload may
  mean unsupported, cached, delayed, or already in the requested state; it is not
  evidence for an enum value.
- Unknown values remain unresolved; the physical label is never inferred from
  numeric ordering.

Nine saved sessions contain the incorporated evidence. Eight completed normally.
The first was preserved as a partial capture after the server closed the socket;
its completed front- and rear-door transitions and final driver-door frame remain
usable. Each saved transition contains the user-supplied candidate label, frame
indexes, changed RVMs, and structural before/after deltas. The probe retains the
label as `candidate_unverified`; the semantic conclusions in this document come
from reviewing those deltas against the supervised physical state.

## Live-state matrix

| State | API capture | HA snapshot | Result or reason |
| --- | --- | --- | --- |
| Parked, asleep, unplugged | `asleep-correlation-minimal`, `asleep-correlation-full` | Registry snapshot captured before HA was stopped | Power state 1 correlated with app sleep; neither the five-second subscription nor the full read inventory visibly woke the R2 |
| Awake and parked | `parked-closed-locked-unplugged` | 27 registered entities | Direct `vehicle.power.state = 3` (`ready`) |
| Climate off | `parked-closed-locked-unplugged` | 27 registered entities | Preconditioning payload empty/default |
| Climate actively preconditioning | `climate-active-preconditioning` | 27 registered entities | Status/type `(2, 1)`; frequent cabin temperature updates |
| Locked and closed | `parked-closed-locked-unplugged` | 27 registered entities | User-confirmed; lock state 1 and closure state 2 correlated |
| Closures and windows | Nine `r2-*-correlation` captures | HA stopped to preserve the account WebSocket | Four doors, frunk, liftgate, and five windows mapped through `body.closures.states`; state 1 open/state 2 closed |
| Locked and unlocked | Physical-correlation baselines and gear session | HA stopped | All six lock records use state 1 locked/state 2 unlocked |
| Stationary P/R/N/D | `r2-gear-drive-mode-correlation` | HA stopped | Gear enum 1/2/3/4 maps to Park/Reverse/Neutral/Drive |
| All seven drive modes | `r2-drive-modes-1`, `r2-drive-modes-2` | HA stopped | Direct enum labels captured; returned to All-Purpose |
| Charge-port flap open/closed | `r2-charge-port-correlation` | HA stopped | No requested RVM changed; no charge-port closure position observed |
| Plugged, not charging | `charging-complete-modern-surfaces` | 27 entities; seven charging unavailable | Charge-complete/plugged variant captured |
| Active AC charging, early | `ac-charging-early` | 27 entities; all seven charging unavailable | Parallax populated power/rate/time/energy; typed stream null |
| Active AC charging, later | `ac-charging-later` | 27 entities; all seven charging unavailable | 1.8 kWh total/1.7 pack/0.1 systems; app correlation captured |
| Immediately after charging stops | `charging-stopped-plugged` | 27 entities; seven charging unavailable | Final totals persist; power/time clear; status and graph change |
| After unplugging | `post-session-unplugged` | 27 registered entities | Live totals/graph cleared; plug/display `(1, 1)`; legacy said `charging_ready` |
| DC fast charging | Follow-up | Follow-up | Not naturally available |
| Active drive and navigation | `active-drive-navigation`, `active-drive-navigation-long` | Production diagnostics observed independently | Direct speed/bearing and Go state; five-second navigation progress with destination, distance, time, and nested motion data |
| OTA installation | Follow-up | Follow-up | No update pending |

## Authentication, errors, and rate behavior

- MFA login and persisted tokens work for the dedicated driver after restart.
- Initial vehicle and driver/key reads succeed for the secondary driver.
- Charging history may be role-limited: the completed summary list was empty.
- Introspection is explicitly disabled on the charging endpoint and returns only
  generic validation failure on the gateway endpoint.
- No rate-limit, temporary-account-lock, or reauthentication response occurred in
  the paced captures.
- Raw Rivian exceptions are unsafe to log because their arguments can include
  request headers. Research plumbing logs only status, operation, GraphQL code,
  and message.

## Validation

- Two complete Home Assistant starts loaded the persisted config entry without
  another MFA challenge and recreated the same 27 R2 entities.
- The second start followed an explicit graceful shutdown. The in-container
  recorder returned `ok` from `PRAGMA quick_check` before and after the restart
  and retained its recorded entity states.
- Both starts logged the expected `getLiveSessionData` validation failure and
  then continued setup through the narrow initial-charging guard. All seven
  charging entities remained `unavailable`, matching the API evidence.
- `ruff check`, `ruff format --check`, `pre-commit run --all-files`, Python bytecode
  compilation, the probe sanitizer self-test, and `git diff --check` pass.
- An exact-value comparison found none of the three stored Rivian authentication
  values in tracked files, the research document, probe source, or sanitized
  captures.
- The development Home Assistant instance was stopped for the interactive
  physical-correlation sessions so it would not compete for the account
  WebSocket. Its persisted-authentication restart validation had already passed.
  Unrelated devcontainer `go2rtc` and `libpcap` warnings do not block Rivian.

## Upstream and ecosystem findings

Upstream `rivian-python-client` after 2.0.0 contains dependency/tooling work but
no released or unreleased Parallax support. Its current main branch still calls
the removed live charging query. A simple rename was rejected because
`getLiveSessionHistory` has an incompatible response shape:
[client PR 178](https://github.com/bretterer/rivian-python-client/pull/178).

The integration's charging failure is also tracked in
[home-assistant-rivian issue 254](https://github.com/bretterer/home-assistant-rivian/issues/254).
An experimental client fork implements the typed `chargingSession` subscription,
but it does not yet establish complete R2/home-AC behavior or Parallax support.
No public Home Assistant fork found during this research has verified,
comprehensive R2 support.

Current independent implementations confirm the need to multiplex one socket and
decode Parallax RVMs, including
[Rivolt's charging subscription](https://github.com/apohor/rivolt/blob/9d0b3362f4f9a0da6d625009a9dd74c6c64e8a28/internal/rivian/ws_charging.go)
and
[Parallax decoder](https://github.com/apohor/rivolt/blob/9d0b3362f4f9a0da6d625009a9dd74c6c64e8a28/internal/rivian/ws_parallax.go).

## Unresolved questions and outstanding captures

- Confirm any newly observed closure positions or enum integers before assigning
  labels. The current physical matrix used one supervised cycle per state rather
  than the originally proposed duplicate cycles.
- Correlate remaining charge-status and climate protobuf enums with controlled
  physical transitions.
- Determine whether the dedicated driver can ever see completed charging detail;
  compare with an owner capture only if necessary.
- Capture a registered wallbox account if one becomes available.
- Capture DC fast charging and OTA installation only when they occur naturally.
- Investigate current server shapes for settings, connected products, and
  departure schedules with additional one-field validation probes if those
  surfaces become relevant.

## Evidence summary for the future implementation phase

1. The small R2 entity set is deterministic model gating, not failed vehicle
   discovery.
2. All seven charging entities are unavailable because their root query was
   removed, not because the R2 returns those fields as null.
3. Legacy `vehicleState` remains schema-compatible but sparse and often stale for
   R2.
4. Parallax is the primary direct R2 telemetry source and supplied 42 focused
   domain topics in one bounded capture.
5. Typed `chargingSession`, live history, completed summaries, OTA details,
   images, configuration, schedules, and estimated range each have different
   current shapes; packaged client schemas cannot be trusted without live
   validation.
6. Official-app and Roamer coverage is consistent with direct Parallax telemetry
   plus repeated-sample derivation, not with a single comprehensive legacy API.
7. The core sleep, awake/parked, closure, five-window, lock, gear, all seven
   drive-mode, active-drive/navigation, climate, charging, stopped, and
   unplugged transitions are captured. The charge-port flap produced no
   observed RVM change. Remaining gaps are naturally occurring rare states and
   unresolved enum details, not discovery of the modern transport.
8. Active driving directly supplies speed and bearing through GNSS. An active
   route additionally supplies five-second remaining-distance/time and nested
   motion updates plus destination and route metadata. Rolling efficiency is
   not directly observed and short-window SOC/odometer derivation is too coarse.
