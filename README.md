# net-probe

An hourly network probe for a home network, written for a host with node_exporter. Every run
pings each target; at fixed hours it runs heavier tests, one at a time: an Ookla speed test, and
10-second iperf3 runs both ways to a target. A heavy test only runs while VictoriaMetrics (or any
Prometheus-compatible query API) shows its path idle, so it neither disrupts real traffic nor
reads it as a slow link. Results, skips and errors go to node_exporter's textfile collector.

`net_probe.py` is one file, Python 3 standard library only, so it runs on a bare host.

## How it schedules

- Each heavy test has `slots`, local hours. It is due when its latest slot is after its last
  success, so it runs once per slot, and a skipped or failed test retries every hour until it
  succeeds or its next slot comes. Keep slots of different tests in different hours so one run
  never carries two of them.
- Before a heavy test, each `busy` query runs as an instant query; any series over its
  `limit_bps` skips the test (`busy`). A failed query doesn't: the test runs anyway.
- An iperf3 test whose target didn't answer a single ping is skipped untried (`unreachable`),
  as is one whose server refuses the connection; a server already running someone else's test
  skips it (`server_busy`). Any other failure is `error`, logged to the journal.
- A probe never fails the run. The state file keeps each probe's last results, so the textfile
  carries them between runs.

## Install

Needs `ping`, `iperf3` (client) and, for the speed test, [Ookla's speedtest
CLI](https://www.speedtest.net/apps/cli). The iperf3 targets each run `iperf3 -s`.

```bash
sudo install -D net_probe.py /usr/local/bin/net_probe
sudo install -D -m 644 config.example.json /etc/net_probe/config.json   # then edit it
sudo install -D -m 644 -t /etc/systemd/system systemd/net_probe.service systemd/net_probe.timer
sudo systemctl daemon-reload
sudo systemctl enable --now net_probe.timer
sudo systemctl start net_probe   # every test is due on the first run
```

To pin a version, fetch the file at a commit and check its hash:

```bash
wget -O net_probe.py https://raw.githubusercontent.com/babraham123/net-probe/COMMIT/net_probe.py
echo "SHA256  net_probe.py" | sha256sum --check
```

## Config

JSON, `/etc/net_probe/config.json` unless `--config PATH` says otherwise.
[`config.example.json`](config.example.json) is a complete one.

| Key | Required | Meaning |
| --- | --- | --- |
| `targets` | yes | Path name → address. Every run pings each one. |
| `isp` | yes | `down_mbps`, `up_mbps`: the plan's advertised speeds, exported as `isp_plan_bits_per_second` to compare the speed test with |
| `tests` | yes | Heavy tests in run order, below |
| `victoriametrics` | with any `busy` | `query_url` (the `/api/v1/query` endpoint), and for basic auth `user` (default `admin`) with `password_file` or `password_command` (an argv list whose stdout is the password) |
| `state_path` | no | Default `/var/lib/net_probe/state.json` |
| `metrics_path` | no | The textfile, default `/var/lib/node_exporter/textfile_collector/net_probe.prom` |
| `metric_prefix` | no | Default `homelab` |
| `speedtest` | no | The Ookla CLI, default `speedtest` on `PATH` |

Each test:

| Key | Meaning |
| --- | --- |
| `test` | `speedtest`, at most once, or `iperf` |
| `path` | `iperf` only: the target to test against |
| `slots` | Local hours, 0 to 23 |
| `busy` | Optional list of `{"query": PROMQL, "limit_bps": N}`; the query returns bit/s |

## Metrics

With the default prefix, `homelab_`:

| Metric | Labels |
| --- | --- |
| `homelab_ping_loss_ratio`, `homelab_ping_rtt_seconds` | `path` |
| `homelab_speedtest_bits_per_second` | `direction` (`down`, `up`) |
| `homelab_speedtest_latency_seconds`, `_jitter_seconds`, `_packet_loss_ratio` | |
| `homelab_speedtest_info` | `server`, `isp` |
| `homelab_isp_plan_bits_per_second` | `direction` |
| `homelab_iperf_up` | `path` |
| `homelab_iperf_bits_per_second`, `homelab_iperf_retransmits` | `path`, `direction` (`up` is from the probe host) |
| `homelab_net_probe_last_attempt_timestamp_seconds`, `_last_success_timestamp_seconds` | `probe` |
| `homelab_net_probe_skipped_total` | `probe`, `reason` (`busy`, `unreachable`, `server_busy`, `error`) |

Probes are `ping_<path>`, `speedtest` and `iperf_<path>`.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT, see [LICENSE](LICENSE).
