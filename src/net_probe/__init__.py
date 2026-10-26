# Network probe, run hourly by a systemd timer. Every run pings each target, then runs, one at
# a time and in config order, each heavy test whose slot has come: the Ookla speed test, and
# iperf3 to a target in both directions. A heavy test is due when its latest slot hour is after
# its last success, so a skipped one retries every run until it succeeds or its next slot
# comes. It skips while a VictoriaMetrics (or any Prometheus-compatible) instant query shows
# its path busy, so it neither disrupts real traffic nor reads it as a slow link. Results,
# skips and errors go to node_exporter's textfile collector; a probe never fails the run.
# Standard library only, shipped as one zipapp built by build.py. The config is JSON; see
# config.example.json and README.md.
