# Bundled observation core

This package is the station-repository copy of `P1/11_演示视觉/lf2s_demo_vision`.
The workbench release is self-contained, so the reviewed frame, parser and
request-lifecycle implementation are included here instead of importing from
the surrounding research workspace. Keep the two copies synchronized when the
standalone observation contract changes.

It reports image observations only. It does not grant crossing permission,
verify physical image accuracy, or expose a flight/action interface.
