# One run: the pings, then each heavy test whose slot has come, and the state they leave.

import json
from datetime import datetime, timedelta
from pathlib import Path

from .metrics import exposition
from .probes import Skip, busy, iperf, speedtest
from .util import log, write


def latest_slot(now, hours):
  """Unix time of the latest slot hour at or before now, in local time."""
  local = datetime.fromtimestamp(now)
  past = [
    local.replace(hour=h, minute=0, second=0, microsecond=0) for h in hours if h <= local.hour
  ]
  if not past:
    past = [(local - timedelta(days=1)).replace(hour=max(hours), minute=0, second=0, microsecond=0)]
  return max(past).timestamp()


def finish(state, probe, now, samples=None, skipped=None):
  s = state.setdefault(probe, {"skipped": {}})
  s["attempt"] = now
  if samples is not None:
    s["samples"] = samples
  if skipped:
    s["skipped"][skipped] = s["skipped"].get(skipped, 0) + 1
    log(f"{probe}: skipped, {skipped}")
  else:
    s["success"] = now


def run(config, now, tools):
  state_path, metrics_path = Path(config["state_path"]), Path(config["metrics_path"])
  state = json.loads(state_path.read_text()) if state_path.exists() else {}
  reachable = {}
  for path, address in config["targets"].items():
    loss, rtt = tools.ping(address)
    reachable[path] = loss < 1
    samples = [["ping_loss_ratio", {"path": path}, loss]]
    if rtt is not None:
      samples.append(["ping_rtt_seconds", {"path": path}, rtt])
    finish(state, f"ping_{path}", now, samples, None if reachable[path] else "unreachable")

  for test in config["tests"]:
    probe, path = test["probe"], test.get("path")
    if state.get(probe, {}).get("success", 0) >= latest_slot(now, test["slots"]):
      continue
    try:
      if path and not reachable[path]:
        raise Skip("unreachable", [["iperf_up", {"path": path}, 0]])
      if busy(tools, test["busy"]):
        raise Skip("busy")
      samples = iperf(tools, path, config["targets"][path]) if path else speedtest(tools)
    except Skip as e:
      finish(state, probe, now, e.samples, e.reason)
    except Exception as e:
      log(f"{probe}: {e!r}")
      finish(state, probe, now, None, "error")
    else:
      finish(state, probe, now, samples)
      log(f"{probe}: ok")

  write(state_path, json.dumps(state, indent=1))
  write(metrics_path, exposition(config, state))
