# The external commands and the metrics query.

import base64
import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path


class Tools:
  """The commands and the metrics query; tests substitute their own."""

  def __init__(self, config):
    self.speedtest_command = config["speedtest"]
    self.vm = config.get("victoriametrics", {})
    self.authorization = None

  def ping(self, address):
    """(loss ratio, average rtt in seconds or None) over 20 pings."""
    out = subprocess.run(
      ["ping", "-c", "20", "-i", "0.2", "-W", "1", "-q", address],
      text=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      timeout=60,
    ).stdout
    counts = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    if not counts or not int(counts[1]):
      return 1.0, None
    rtt = re.search(r"= [\d.]+/([\d.]+)/", out)
    return 1 - int(counts[2]) / int(counts[1]), float(rtt[1]) / 1000 if rtt else None

  def speedtest(self):
    command = [self.speedtest_command, "--format=json", "--accept-license", "--accept-gdpr"]
    return json.loads(
      subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, timeout=180).stdout
    )

  def iperf(self, address, reverse):
    """iperf3's JSON report; on failure it carries only an error."""
    command = ["iperf3", "-c", address, "-J", "-t", "10", "-O", "2", "--connect-timeout", "3000"]
    out = subprocess.run(
      command + (["-R"] if reverse else []), text=True, stdout=subprocess.PIPE, timeout=60
    ).stdout
    return json.loads(out)

  def basic_auth(self):
    """The Authorization header value, or "" without a password."""
    if "password_command" in self.vm:
      password = subprocess.run(
        self.vm["password_command"], check=True, text=True, stdout=subprocess.PIPE
      ).stdout.rstrip("\n")
    elif "password_file" in self.vm:
      password = Path(self.vm["password_file"]).read_text().rstrip("\n")
    else:
      return ""
    credentials = f"{self.vm.get('user', 'admin')}:{password}".encode()
    return "Basic " + base64.b64encode(credentials).decode()

  def query(self, expr):
    """The values of an instant query."""
    if self.authorization is None:
      self.authorization = self.basic_auth()
    headers = {"Authorization": self.authorization} if self.authorization else {}
    url = f"{self.vm['query_url']}?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(
      urllib.request.Request(url, headers=headers), timeout=10
    ) as response:
      return [float(r["value"][1]) for r in json.load(response)["data"]["result"]]
