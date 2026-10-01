# discord — username sniper

Python sniper for short Discord usernames.

## Setup
1. Fill `proxies.txt` — one proxy per line (residential recommended).
2. Edit `config.json` — username length, target, delay, concurrency.
3. Fill `wordlist.txt` — one candidate per line.

## Run
python oxy.py

## Notes
- Proxy rotation is per-request.
- Uses `requests` + raw HTTP.
- No external service needed.
