#!/usr/bin/env python
"""
ddns_update.py - keep theleague.letsfriggen.party's A record pointed at this network.

Run every 30 minutes by Task Scheduler ("Rockwood DDNS Update").

	python ddns_update.py              # update the record if the public IP changed
	python ddns_update.py --dry-run    # look up both and print what would change; no edit call

Each run:
	1. Public IPv4 from Porkbun's ping (api-ipv4.porkbun.com, the address Porkbun sees), checked
	   against api.ipify.org. If they disagree, or either isn't a public IPv4, nothing is changed.
	2. The current A record from Porkbun's API (retrieveByNameType), not DNS, so a stale resolver
	   cache can't mislead it.
	3. Only if they differ: editByNameType sets the A record to the public IP.

Keys come from .env (PORKBUN_API_KEY, PORKBUN_SECRET_KEY) and are never printed or logged; every
log line also goes through weekly_maintenance.redacted(). One line per run goes to the gitignored
ddns_update.log. ddns_update.state (gitignored) counts consecutive failures; on the 3rd in a row a
message goes to NOTIFY_WEBHOOK_URL, and another when it recovers.

Exit codes: 0 ok (unchanged, updated, or dry run), 1 failed.
"""

import argparse
import ipaddress
import json
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from weekly_maintenance import redacted  # noqa: E402  (same .env-value redaction as the weekly task)

DOMAIN = "letsfriggen.party"
SUBDOMAIN = "theleague"
TTL = "600"
API = "https://api.porkbun.com/api/json/v3"
PING = "https://api-ipv4.porkbun.com/api/json/v3/ping"
IPIFY = "https://api.ipify.org"
LOG = os.path.join(HERE, "ddns_update.log")
STATE = os.path.join(HERE, "ddns_update.state")
NOTIFY_AFTER = 3
TIMEOUT = 15


class DDNSError(Exception):
	pass


def credentials():
	key, secret = os.getenv("PORKBUN_API_KEY"), os.getenv("PORKBUN_SECRET_KEY")
	if not key or not secret:
		raise DDNSError("PORKBUN_API_KEY / PORKBUN_SECRET_KEY missing from .env")
	return {"apikey": key, "secretapikey": secret}


def porkbun(url, body):
	"""POST to Porkbun; return the JSON on status SUCCESS. Errors never include the request body."""
	try:
		r = requests.post(url, json=body, timeout=TIMEOUT)
	except requests.RequestException as e:
		raise DDNSError(f"{url.split('/json/v3/')[-1]}: {type(e).__name__}")
	try:
		data = r.json()
	except ValueError:
		raise DDNSError(f"{url.split('/json/v3/')[-1]}: HTTP {r.status_code}, not JSON")
	if data.get("status") != "SUCCESS":
		raise DDNSError(f"{url.split('/json/v3/')[-1]}: HTTP {r.status_code}, {data.get('message', 'no message')}")
	return data


def public_ipv4(ip, source):
	try:
		addr = ipaddress.ip_address(ip)
	except ValueError:
		raise DDNSError(f"{source} returned {ip!r}, not an IP address")
	if addr.version != 4 or not addr.is_global:
		raise DDNSError(f"{source} returned {ip}, not a public IPv4 address")
	return ip


def current_ip(creds):
	seen_by_porkbun = public_ipv4(porkbun(PING, creds).get("yourIp", ""), "Porkbun ping")
	try:
		seen_by_ipify = public_ipv4(requests.get(IPIFY, timeout=TIMEOUT).text.strip(), "ipify")
	except requests.RequestException as e:
		raise DDNSError(f"ipify: {type(e).__name__}")
	if seen_by_porkbun != seen_by_ipify:
		raise DDNSError(f"public IP sources disagree (Porkbun {seen_by_porkbun}, ipify {seen_by_ipify}); not changing anything")
	return seen_by_porkbun


def current_record(creds):
	records = porkbun(f"{API}/dns/retrieveByNameType/{DOMAIN}/A/{SUBDOMAIN}", creds).get("records") or []
	if len(records) != 1:
		raise DDNSError(f"expected 1 A record for {SUBDOMAIN}.{DOMAIN}, Porkbun has {len(records)}")
	return records[0]["content"]


def update_record(creds, ip):
	porkbun(f"{API}/dns/editByNameType/{DOMAIN}/A/{SUBDOMAIN}", {**creds, "content": ip, "ttl": TTL})


# --------------------------------------------------------------------------- #
# log, failure count, notification
# --------------------------------------------------------------------------- #
def log(status, detail):
	stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
	line = redacted(f"{stamp} {status:<9} {detail}")
	with open(LOG, "a", encoding="ascii", errors="replace") as f:
		f.write(line + "\n")
	print(line)
	return line


def failures():
	try:
		with open(STATE) as f:
			return int(json.load(f).get("consecutive_failures", 0))
	except (OSError, ValueError):
		return 0


def set_failures(n):
	with open(STATE, "w") as f:
		json.dump({"consecutive_failures": n, "updated": datetime.now(timezone.utc).isoformat()}, f)


def notify(text):
	hook = os.getenv("NOTIFY_WEBHOOK_URL")
	if not hook:
		return
	text = redacted(text)[:1900]
	body = {"json": {"content": text}} if "discord.com" in hook else \
	       {"json": {"text": text}} if "hooks.slack.com" in hook else {"data": text.encode("utf-8")}
	try:
		requests.post(hook, timeout=TIMEOUT, **body).raise_for_status()
	except requests.RequestException as e:
		log("notify", f"FAIL: {type(e).__name__}")    # never the URL


def main():
	ap = argparse.ArgumentParser(description="Point theleague.letsfriggen.party at this network's public IP.")
	ap.add_argument("--dry-run", action="store_true", help="look up both and print what would change; no edit call")
	dry = ap.parse_args().dry_run
	name = f"{SUBDOMAIN}.{DOMAIN}"

	try:
		creds = credentials()
		ip = current_ip(creds)
		record = current_record(creds)
		if ip == record:
			log("dry-run" if dry else "unchanged", f"{name} A {record} = public IP")
		elif dry:
			log("dry-run", f"{name} A {record} -> would change to {ip}")
		else:
			update_record(creds, ip)
			log("updated", f"{name} A {record} -> {ip}")
	except DDNSError as e:
		line = log("FAIL", f"{name}: {e}")
		if dry:
			return 1
		n = failures() + 1
		set_failures(n)
		if n == NOTIFY_AFTER:
			notify(f"Rockwood DDNS: {n} failures in a row for {name}.\n{line}")
		return 1

	if not dry:
		if failures() >= NOTIFY_AFTER:
			notify(f"Rockwood DDNS: {name} recovered after {failures()} failures.")
		set_failures(0)
	return 0


if __name__ == "__main__":
	sys.exit(main())
