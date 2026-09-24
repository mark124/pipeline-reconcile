# Security

## Reporting a vulnerability

Email `mark@rowset.co`. Please do not open a public issue for a security problem.

Include what you ran, what happened, and what you expected. A reproduction is welcome but not
required — a clear description is enough to start.

## What this plugin does, so you can judge the risk yourself

`reconcile.py` and `make_fixtures.py` are Python standard library only. There are no dependencies to
audit and nothing to install.

- **It reads the files you name on the command line.** Source, destination, baseline and journal are
  opened read-only.
- **It writes only where you tell it to.** `--json-out` writes the finding set to the path you give,
  and refuses with exit 2 if that path resolves to one of the inputs, so a typo cannot overwrite an
  export. Nothing is read before that check runs.
  `make_fixtures.py` writes synthetic data to `--out`, which defaults to `./fixtures`. Nothing else
  on disk is touched.
- **It opens no network connections.** There is no telemetry, no update check, and no outbound
  request of any kind.
- **It takes no credentials.** It works on exports, not connection strings, so it has no way to
  reach a production system even if you wanted it to.

## What to be careful about

The files you point it at are your data. If you use `--json-out`, the output contains **natural key
values** for every finding — student IDs, order numbers, account numbers, whatever your key is. Treat
that file with the same care as the exports it came from, and don't paste it into a ticket that is
more widely readable than the source system.

`--show` prints keys to the terminal for the same reason. Set `--show 0` if you are screen-sharing.

## Supported versions

The latest release is the supported one. This is a single-maintainer project; fixes go onto `main`.
