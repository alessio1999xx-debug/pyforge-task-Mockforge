# MockForge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

MockForge is a mock HTTP API in one Python file. It uses the standard library only, so there is nothing to `pip install`. The process keeps an in-memory JSON store for CRUD routes and fills string templates (`{{uuid}}`, `{{name}}`, `{{int:min:max}}`, and the macros below) when a request hits a template route. Latency and a random HTTP 500 rate can be set on those template routes.

Requires Python 3.8 or newer. The examples below were run with Python 3.12. `requirements.txt` records that there are no third-party dependencies.

## Quick start

From this directory:

```bash
python3 main.py start
```

That starts the built-in demo on `127.0.0.1:8080`. `Ctrl+C` stops it and closes the port. `--port` chooses another port. `--host 0.0.0.0` listens on all interfaces.

In another shell:

```bash
curl -s http://127.0.0.1:8080/api/v1/customers
curl -s http://127.0.0.1:8080/api/v1/orders
curl -s http://127.0.0.1:8080/api/v1/analytics/realtime
curl -s -X POST http://127.0.0.1:8080/api/v1/gateway/charge \
  -H 'Content-Type: application/json' \
  -d '{"amount": 19.99}'
```

```bash
python3 main.py export-spec
```

`export-spec` builds the same demo and prints an OpenAPI 3.0 path list from those routes, then exits. The list includes customers and orders (GET and POST on the collection, GET, PUT, PATCH, and DELETE on an item), `/api/v1/analytics/realtime`, `/api/v1/gateway/charge`, and the three `/_mockforge/` routes. The charge operation notes its `0.25` failure probability. Response schemas are omitted. `--port` and `--host` do not change the document. From Python, `forge.export_spec()` does the same for whatever you registered on that instance.

## Demo routes

`start` builds this server in `build_enterprise_demo()` and serves it. Seed data is created once at startup. Template routes render again on every request.

| Methods | Path | Behavior |
| --- | --- | --- |
| GET, POST | `/api/v1/customers` and `/api/v1/customers/` | Collection. Starts with 5 records. |
| GET, PUT, PATCH, DELETE | `/api/v1/customers/{id}` | One customer. Trailing slash is accepted. |
| GET, POST | `/api/v1/orders` and `/api/v1/orders/` | Collection. Starts with 3 records. |
| GET, PUT, PATCH, DELETE | `/api/v1/orders/{id}` | One order. |
| GET | `/api/v1/analytics/realtime` | Template. Sleeps 15–45 ms, then returns new JSON. |
| POST | `/api/v1/gateway/charge` | Template. Sleeps 150–450 ms. About 25% of calls return HTTP 500. |

Each seeded customer has `name`, `email`, `company`, `account_tier` (`Starter`, `Professional`, or `Enterprise`), `status` (`ACTIVE`), a generated `id`, and `_created_at`. Name, email, and company are separate draws. A customer email has the form `user321@quantixlabs.net`.

Each seeded order has `transaction_code` (`TXN-` plus six digits), `amount` (a JSON number from `DataForge.currency(50, 2500)`, so 50 up to 2500), `status` (`COMPLETED`), `id`, and `_created_at`.

`GET /api/v1/customers` returns `{"data": [...], "total": 5}`. One customer from a run:

```json
{
  "name": "Devon Nakamura",
  "email": "user321@quantixlabs.net",
  "company": "HyperDrive AI",
  "account_tier": "Starter",
  "status": "ACTIVE",
  "id": "ad68a952-2edc-4c70-9b5b-6f48f25e6509",
  "_created_at": "2026-10-20T11:27:50.689638+00:00"
}
```

Those values are generated again at each process start. `GET /api/v1/analytics/realtime` renders a new body on every call. `active_users` is a JSON number because that template value is exactly `{{int:150:850}}`. `mrr` stays text because `{{currency}}` always expands to text. `load_factor` stays text because of the `%` suffix. One response has this shape:

```json
{
  "session_id": "8f5c0c2e-1b4a-4e5d-9c0a-6e7f8a9b0c1d",
  "server_time": "2026-10-20T12:00:00.000000+00:00",
  "active_users": 489,
  "mrr": "741.23",
  "status": "HEALTHY",
  "infrastructure": { "datacenter": "us-east-1", "load_factor": "80%" }
}
```

`POST /api/v1/gateway/charge` parses a JSON body and discards it. The response is independent of `amount`. A successful call returns `charge_id` (a new UUID string), `status` (`SETTLED`), and `verified` (`true`). A failed call returns HTTP 500:

```json
{
  "error": "InjectedChaosFailure",
  "message": "Chaos fault injection triggered by contract specification.",
  "timestamp": "2026-10-20T11:27:56.657734+00:00"
}
```

Unknown paths return HTTP 404 with `error` set to `Contract Unmatched`.

These routes also exist on every server, including one you build in code. They are matched before your routes and are omitted from the metrics and the request log:

| Method and path | Response |
| --- | --- |
| `GET /_mockforge/routes` | Registered routes: method, path template, regex, action, and a stateful flag. |
| `GET /_mockforge/logs` | The last 50 entries from a buffer of 200 (method, path, status, latency, timestamp). Ordinary responses, stateful 404s, unmatched 404s, injected HTTP 500s, and template errors are all recorded. |
| `GET /_mockforge/metrics` | `uptime_seconds`, `total_requests`, `total_failures`, and `avg_latency_ms`. |

Average latency includes the artificial sleep. Unmatched 404s and injected 500s count toward the totals. Fetching `/_mockforge/metrics`, `/_mockforge/routes`, or `/_mockforge/logs` does not.

Stateful demo routes sleep for a random 10–60 ms on every request, including writes.

## Use it from Python

`main.py` is the module name. Import it from the directory that contains the file, or put the file on `PYTHONPATH`.

```python
from main import MockForge, DataForge

forge = MockForge(port=8080, host="127.0.0.1")

# GET/POST /api/v1/widgets
# GET/PUT/PATCH/DELETE /api/v1/widgets/{id}
forge.resource("widgets")
forge.seed("widgets", 2, lambda: {
    "name": DataForge.name(),
    "email": DataForge.email(),
    "company": DataForge.company(),
})

forge.mock(
    "/api/v1/health",
    method="GET",
    response={
        "id": "{{uuid}}",
        "status": "{{status}}",
        "active_users": "{{int:150:850}}",
        "checked_at": "{{iso_date}}",
        "ok": True,
    },
    latency_ms=(5, 20),
    failure_rate=0.0,
)

forge.serve()  # blocks until Ctrl+C
```

In that health template, `active_users` is a JSON number on the wire. `id`, `status`, and `checked_at` stay strings.

`resource(name, base_path=None)` registers six routes. The default base path is `/api/v1/{name}`. Collection URLs accept an optional trailing slash. Item URLs capture `{id}` as any segment except `/`.

`mock(path, method="GET", response=None, latency_ms=(10, 50), failure_rate=0.0)` registers one template route. `{param}` in `path` becomes a named path segment. `failure_rate` is the probability of HTTP 500, checked after the sleep and before the template is rendered. `latency_ms` is a `(min, max)` sleep in milliseconds (`random.uniform`).

`seed(name, count, generator)` calls `generator` `count` times and inserts each dict. Anything you pass as `id` is kept; otherwise the store assigns a UUID. Creation adds `_created_at`. Updates add `_updated_at` and force `id` back to the URL id.

`POST` stores the JSON object. `PUT` and `PATCH` both merge keys into the stored object, and both leave keys you omit in place. `DELETE` of an existing id returns HTTP 204 with an empty body. A missing id returns HTTP 404 `NotFound`. A list is `{"data": [...], "total": N}`. A single record is the object itself. Query strings are stripped and ignored.

`DataForge` can also be called directly: `name()`, `email(name=None)`, `company()`, `status()`, and `currency(min_val=10.0, max_val=10000.0)`. `currency()` returns a float. `email("Casey O'Connor")` keeps letters only, so the local part is `caseyoconnor`.

## Template macros

Macros run only inside strings, and only as exact tokens. Numbers, booleans, and nulls are copied through, which is why `"ok": True` stays a JSON boolean. Each occurrence is rendered on its own, including two tokens in one string.

| Token | Inserted text |
| --- | --- |
| `{{uuid}}` | A UUID4. |
| `{{name}}` | First and last name from fixed lists of 10 and 9 names. |
| `{{email}}` | `user` + a number from 100 to 999, `@`, a company name in lowercase with spaces removed, `.`, and one of `com`, `io`, `tech`, `net`, `org`. The sibling `{{name}}` field is a different draw. |
| `{{company}}` | One of ApexScale, OmniCloud, HyperDrive AI, Starlight Pay, Quantix Labs, VectorSec. |
| `{{status}}` | One of `ACTIVE`, `PENDING`, `PROCESSING`, `FLAGGED`, `COMPLETED`, `SUSPENDED`. |
| `{{iso_date}}` | Current UTC time from `datetime.now(timezone.utc).isoformat()`. |
| `{{currency}}` | `str(DataForge.currency())`: `random.uniform(10, 10000)` rounded to two decimals. The token takes no range. `DataForge.currency(min_val, max_val)` accepts a range only when called from Python. |
| `{{int:min:max}}` | `random.randint(min, max)`. Both ends are inclusive. The value is a JSON number when the string is only that token, and text when the token sits inside other characters. |

`{{int:1:1}}-{{int:2:2}}` becomes the text `1-2`. Two `{{uuid}}` tokens in one string are different ids. The same is true for `{{currency}}` and the other tokens. If `min` is greater than `max`, the request returns HTTP 400:

```json
{ "error": "TemplateError", "message": "{{int:5:1}} is invalid because 5 is greater than 1." }
```

The server keeps running. Calling `DataForge.synthesize_template("{{int:5:1}}")` raises `TemplateError`.

## Other details

- Responses that have a body are pretty-printed JSON (`Content-Type: application/json; charset=utf-8`) with `Access-Control-Allow-Origin: *`. The status line is HTTP/1.0. `OPTIONS` returns 204 and the same CORS headers.
- The server is `ThreadingMixIn`: one thread per request, daemon threads, no pool cap. `allow_reuse_address` is on, so a restart can bind the same port immediately.
- `POST`, `PUT`, and `PATCH` read `Content-Length` bytes before the chaos roll. Valid JSON becomes the body object. Any other payload becomes `{"_raw": "<text>"}`. An injected HTTP 500 does not echo that body.
- `mock()` compiles `path` as a regular expression after substituting `{placeholders}`. Characters such as `.` are still regex syntax.
- There is no authentication, TLS, pagination, schema check, or persistence. Each `MockForge` has its own store, route list, request log, and counters.

## Tests

```bash
python3 -m unittest test_main.py
```

The tests use only the standard library. They cover CRUD, chaos failures, template macros, separate instances, route export, and Ctrl+C shutdown.

## PyForge

MockForge is one of the tools in PyForge ([https://py-forge.com](https://py-forge.com)), a growing catalog of zero-dependency developer micro-tools. This one is free and MIT-licensed. Want the rest? The All-Access Pass includes every current and future tool: $99 now, $149 once we reach 40 tools. [https://py-forge.com/?utm_source=github&utm_medium=readme&utm_campaign=showhn_mockforge](https://py-forge.com/?utm_source=github&utm_medium=readme&utm_campaign=showhn_mockforge)

## License

License: MIT. Copyright (c) 2026 Alessio Auretto. See [LICENSE](LICENSE).
