# The textfile in Prometheus's exposition format.

# Metric names after the prefix, in exposition order
HELP = {
  "ping_loss_ratio": ("gauge", "Share of 20 pings to the path that got no reply."),
  "ping_rtt_seconds": ("gauge", "Average round trip of the replied pings."),
  "speedtest_bits_per_second": (
    "gauge",
    "Ookla speed test throughput; direction down is the download.",
  ),
  "isp_plan_bits_per_second": ("gauge", "The ISP plan's advertised speed (config isp)."),
  "speedtest_latency_seconds": ("gauge", "Ookla speed test idle latency."),
  "speedtest_jitter_seconds": ("gauge", "Ookla speed test idle jitter."),
  "speedtest_packet_loss_ratio": (
    "gauge",
    "Ookla speed test packet loss, when the server reports it.",
  ),
  "speedtest_info": ("gauge", "The last speed test's server and ISP."),
  "iperf_up": ("gauge", "1 when the path's iperf3 server answered, 0 when it was unreachable."),
  "iperf_bits_per_second": (
    "gauge",
    "iperf3 throughput as received; direction up is from this host.",
  ),
  "iperf_retransmits": ("gauge", "TCP retransmits by the iperf3 sender."),
  "net_probe_last_attempt_timestamp_seconds": (
    "gauge",
    "Unix time the probe last ran or was skipped.",
  ),
  "net_probe_last_success_timestamp_seconds": ("gauge", "Unix time the probe last succeeded."),
  "net_probe_skipped_total": ("counter", "Probes skipped or failed, by reason."),
}


def exposition(config, state):
  isp = config["isp"]
  series = {
    "isp_plan_bits_per_second": [
      ({"direction": "down"}, isp["down_mbps"] * 1e6),
      ({"direction": "up"}, isp["up_mbps"] * 1e6),
    ]
  }
  for probe, s in sorted(state.items()):
    for name, labels, value in s.get("samples", []):
      series.setdefault(name, []).append((labels, value))
    series.setdefault("net_probe_last_attempt_timestamp_seconds", []).append(
      ({"probe": probe}, s["attempt"])
    )
    if "success" in s:
      series.setdefault("net_probe_last_success_timestamp_seconds", []).append(
        ({"probe": probe}, s["success"])
      )
    for reason, n in sorted(s["skipped"].items()):
      series.setdefault("net_probe_skipped_total", []).append(
        ({"probe": probe, "reason": reason}, n)
      )
  lines = []
  for name, (kind, text) in HELP.items():
    if name not in series:
      continue
    full = f"{config['metric_prefix']}_{name}"
    lines += [f"# HELP {full} {text}", f"# TYPE {full} {kind}"]
    for labels, value in series[name]:
      escaped = (v.replace("\\", "\\\\").replace('"', '\\"') for v in labels.values())
      label_text = ",".join(f'{k}="{v}"' for k, v in zip(labels, escaped))
      lines.append(full + ("{" + label_text + "}" if labels else "") + f" {value:.10g}")
  return "".join(f"{line}\n" for line in lines)
