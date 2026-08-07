"""``hermes openalex ...``, the terminal surface.

``doctor`` is the one that earns its keep. On a metered API the questions are
"is my key working", "how much is left today", and "what did this session
spend", and answering those at a prompt beats asking an agent to introspect
its own configuration.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import config as config_mod
from . import pricing
from .budget import tracker
from .client import get_client, reset_client, unwrap_rate_limit
from .errors import OpenAlexError

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    _console: Console | None = Console()
except Exception:  # pragma: no cover - rich is a Hermes core dependency
    _console = None
    Console = Panel = Table = None  # type: ignore


def _say(*parts: Any) -> None:
    if _console is not None:
        _console.print(*parts)
    else:
        print(*parts)


def _num(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def _usd(value: Any) -> str:
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _mask(secret: str | None) -> str:
    if not secret:
        return "(not set, anonymous tier)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"


def _status(ok: bool, warn: bool = False) -> str:
    if warn:
        return "[yellow]warn[/yellow]" if _console else "warn"
    return "[green]ok[/green]" if ok else "[red]fail[/red]"


def build_parser(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="openalex_command")

    setup_p = subs.add_parser("setup", help="Store and validate an OpenAlex API key")
    setup_p.add_argument(
        "--key", default=None, help="Provide the key directly instead of being prompted"
    )

    subs.add_parser("doctor", help="Diagnose configuration, connectivity and budget")
    subs.add_parser("budget", help="Show the session ledger and account balance")
    subs.add_parser("prices", help="Show what each kind of call costs")

    get_p = subs.add_parser("get", help="Fetch one record by id, DOI, ORCID or ROR (free)")
    get_p.add_argument("identifier")
    get_p.add_argument("--verbosity", choices=list(config_mod.VALID_VERBOSITY), default=None)
    get_p.add_argument("--json", action="store_true")

    resolve_p = subs.add_parser("resolve", help="Turn a name into an id (free)")
    resolve_p.add_argument("query")
    resolve_p.add_argument("--entity", default=None, choices=list(config_mod.ENTITIES))
    resolve_p.add_argument("--json", action="store_true")

    count_p = subs.add_parser("count", help="Count and group, the cheap call")
    count_p.add_argument("--filter", default=None)
    count_p.add_argument("--search", default=None)
    count_p.add_argument("--group-by", dest="group_by", default=None)
    count_p.add_argument("--entity", default="works", choices=list(config_mod.ENTITIES))
    count_p.add_argument("--json", action="store_true")

    search_p = subs.add_parser("search", help="Search and return records (costs 10x a count)")
    search_p.add_argument("query")
    search_p.add_argument("--field", dest="search_field", default="title_and_abstract")
    search_p.add_argument("--filter", default=None)
    search_p.add_argument("--sort", default=None)
    search_p.add_argument("--limit", type=int, default=10)
    search_p.add_argument("--entity", default="works", choices=list(config_mod.ENTITIES))
    search_p.add_argument("--json", action="store_true")

    fulltext_p = subs.add_parser(
        "fulltext",
        help="Report free if full text exists; download only with --confirm ($0.01)",
    )
    fulltext_p.add_argument("identifier", help="OpenAlex id, DOI, PMID, …")
    fulltext_p.add_argument(
        "--confirm",
        action="store_true",
        help="Spend $0.01 and download text (default: free availability probe only)",
    )
    fulltext_p.add_argument(
        "--format",
        dest="content_format",
        default="text",
        choices=["text", "pdf"],
        help="text (default, GROBID when available) or pdf (link only)",
    )
    fulltext_p.add_argument("--json", action="store_true")

    profile_p = subs.add_parser("profile", help="Show or set the tool profile")
    profile_p.add_argument(
        "value", nargs="?", choices=list(config_mod.VALID_PROFILES), default=None
    )

    cache_p = subs.add_parser("cache", help="Inspect or clear the lookup cache")
    cache_p.add_argument("action", nargs="?", choices=["stats", "clear"], default="stats")


def run_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "openalex_command", None)
    if not sub:
        _say(
            "usage: hermes openalex "
            "{setup,doctor,budget,prices,get,resolve,count,search,fulltext,profile,cache}"
        )
        return 2

    handlers = {
        "setup": cmd_setup,
        "doctor": cmd_doctor,
        "budget": cmd_budget,
        "prices": cmd_prices,
        "get": cmd_get,
        "resolve": cmd_resolve,
        "count": cmd_count,
        "search": cmd_search,
        "fulltext": cmd_fulltext,
        "profile": cmd_profile,
        "cache": cmd_cache,
    }
    handler = handlers.get(sub)
    if handler is None:
        _say(f"Unknown subcommand: {sub}")
        return 2
    try:
        return handler(args)
    except OpenAlexError as exc:
        _say(f"[red]{exc.message}[/red]" if _console else f"error: {exc.message}")
        if exc.next_step:
            _say(f"  {exc.next_step}")
        return 1
    except KeyboardInterrupt:
        return 130


def _call(handler: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(handler(payload))


# --- setup ------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    key = args.key
    if not key:
        import getpass

        _say("OpenAlex API key. Free, from https://openalex.org/settings/api")
        _say("It raises the daily budget from $0.10 to $1.00.")
        try:
            key = getpass.getpass("  key: ").strip()
        except (EOFError, KeyboardInterrupt):
            _say("\nCancelled.")
            return 130
    if not key:
        _say("No key given, nothing changed.")
        return 1

    _say("Checking the key...")
    from .client import OpenAlexClient

    probe = OpenAlexClient(config_mod.OpenAlexConfig(api_key=key))
    try:
        info = unwrap_rate_limit(
            probe.get("rate-limit", cacheable=False, call_class=pricing.SINGLETON)
        )
    except OpenAlexError as exc:
        _say(f"[red]Rejected:[/red] {exc.message}" if _console else f"Rejected: {exc.message}")
        _say("  Nothing was saved.")
        return 1
    finally:
        probe.close()

    try:
        from hermes_cli.config import save_env_value

        save_env_value("OPENALEX_API_KEY", key)
    except Exception as exc:
        _say(f"[red]Could not write .env: {exc}[/red]")
        return 1

    import os

    os.environ["OPENALEX_API_KEY"] = key
    config_mod.reset()
    reset_client()

    _say(
        f"[green]Saved.[/green] Daily budget "
        f"{_usd(info.get('daily_budget_usd'))}, "
        f"{_usd(info.get('daily_remaining_usd'))} remaining."
    )
    return 0


# --- diagnostics ------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    config_mod.reset()
    reset_client()
    cfg = config_mod.load(refresh=True)
    rows: list[tuple] = []
    problems: list[str] = []

    enabled = _plugin_enabled()
    rows.append(("plugin enabled", _status(enabled), "plugins.enabled in config.yaml"))
    if not enabled:
        problems.append("Run: hermes plugins enable openalex")

    rows.append(
        (
            "api key",
            _status(True, warn=not cfg.has_key),
            f"${cfg.api_key_env} = {_mask(cfg.api_key)}",
        )
    )
    if not cfg.has_key:
        problems.append(
            "Optional: hermes openalex setup, for a 10x larger daily budget ($1.00 vs $0.10)"
        )

    client = get_client(cfg)

    # A free singleton proves reachability without spending anything.
    try:
        client.get("works/W2741809807", params={"select": "id"}, cacheable=False)
        rows.append(("connectivity", _status(True), "reached api.openalex.org (free call)"))
    except OpenAlexError as exc:
        rows.append(("connectivity", _status(False), exc.message))
        problems.append("Check network access and proxy settings.")

    if cfg.has_key:
        try:
            info = unwrap_rate_limit(
                client.get("rate-limit", cacheable=False, call_class=pricing.SINGLETON)
            )
            remaining = info.get("daily_remaining_usd")
            rows.append(("key valid", _status(True), "accepted"))
            rows.append(
                (
                    "daily budget",
                    _status(bool(remaining), warn=not remaining),
                    f"{_usd(remaining)} left of {_usd(info.get('daily_budget_usd'))}, "
                    f"resets {info.get('resets_at') or 'at midnight UTC'}",
                )
            )
            if pricing.refresh_from_account(info.get("endpoint_costs_usd")):
                rows.append(("prices", "", "refreshed live from the account"))
            if not remaining:
                problems.append(
                    "Daily budget spent. Free calls (get, resolve) still work until midnight UTC."
                )
        except OpenAlexError as exc:
            rows.append(("key valid", _status(False), exc.message))
            problems.append("Run: hermes openalex setup (the stored key was rejected)")
    else:
        rows.append(("daily budget", "", f"{_usd(pricing.ANON_DAILY_USD)}/day, anonymous tier"))

    observed = client.meter.snapshot()
    if observed.get("account_daily_remaining_usd") is not None:
        rows.append(
            (
                "observed balance",
                "",
                _usd(observed["account_daily_remaining_usd"]) + " (from headers)",
            )
        )

    visible = sorted(cfg.visible_tools())
    rows.append(("profile", "", f"{cfg.profile} ({len(visible)} tools)"))
    rows.append(("verbosity", "", cfg.verbosity))
    rows.append(("rate limit", "", f"{cfg.rate_limit_per_second}/s (API ceiling is 10/s)"))
    rows.append(
        (
            "session budget",
            "",
            f"{_usd(cfg.budget.usd_per_session)} per session",
        )
    )
    rows.append(
        (
            "text classification",
            _status(True, warn=not cfg.budget.allow_text_classification),
            "enabled" if cfg.budget.allow_text_classification else "disabled ($0.01/call)",
        )
    )
    rows.append(
        (
            "cache",
            "",
            f"{'on' if cfg.cache.enabled else 'off'}, ttl {cfg.cache.ttl_seconds}s, "
            f"{client.cache.stats()['entries']} entries",
        )
    )

    if _console is not None and Table is not None:
        table = Table(title="hermes openalex doctor", box=None, padding=(0, 2))
        for column in ("check", "", "detail"):
            table.add_column(column)
        for row in rows:
            table.add_row(*row)
        _console.print(table)
        _console.print(f"\ntools: {', '.join(visible)}")
    else:
        for name, state, detail in rows:
            print(f"{name:22} {state:6} {detail}")
        print("tools:", ", ".join(visible))

    if problems:
        _say("\n[bold]Next steps[/bold]" if _console else "\nNext steps")
        for item in problems:
            _say(f"  - {item}")
        return 0 if enabled else 1

    _say("\n[green]Everything checks out.[/green]" if _console else "\nEverything checks out.")
    return 0


def _plugin_enabled() -> bool:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        return False
    return "openalex" in ((cfg.get("plugins") or {}).get("enabled") or [])


def cmd_budget(args: argparse.Namespace) -> int:
    from .handlers_core import openalex_account

    result = _call(openalex_account, {})
    _say(json.dumps(result, indent=2))
    return 0


def cmd_prices(args: argparse.Namespace) -> int:
    cfg = config_mod.load(refresh=True)
    prices = pricing.snapshot()
    if _console is None or Table is None:
        print(json.dumps(prices, indent=2))
        return 0
    table = Table(title="OpenAlex call prices", box=None, padding=(0, 2))
    table.add_column("call class")
    table.add_column("usd", justify="right")
    table.add_column("calls per anonymous day", justify="right")
    for name, price in sorted(prices.items(), key=lambda kv: kv[1]):
        per_day = "unlimited" if price <= 0 else f"{int(pricing.ANON_DAILY_USD / price):,}"
        table.add_row(name, f"${price:.4f}", per_day)
    _console.print(table)
    _console.print(
        f"\nDaily budget: {_usd(pricing.ANON_DAILY_USD)} anonymous, "
        f"{_usd(pricing.KEYED_DAILY_USD)} with a free key. Resets at midnight UTC."
    )
    _console.print(f"Session cap: {_usd(cfg.budget.usd_per_session)}")
    return 0


# --- lookups ----------------------------------------------------------------


def cmd_get(args: argparse.Namespace) -> int:
    from .handlers_core import openalex_get

    result = _call(openalex_get, {"id": args.identifier, "verbosity": args.verbosity})
    if args.json or _console is None:
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        _say(f"[red]{result.get('error')}[/red]")
        return 1

    record = result.get("record") or (result.get("records") or [{}])[0]
    if not record:
        _say("No record.")
        return 1

    header = " · ".join(
        str(p)
        for p in [
            record.get("title") or record.get("name"),
            record.get("year"),
            record.get("venue"),
        ]
        if p
    )
    _say(Panel(header, title=record.get("id") or "record", expand=False) if Panel else header)
    for label, key in (
        ("doi", "doi"),
        ("type", "type"),
        ("citations", "cited_by_count"),
        ("open access", "open_access"),
        ("topic", "topic"),
        ("h-index", "h_index"),
        ("works", "works_count"),
    ):
        if record.get(key) is not None:
            _say(f"  {label:12} {record[key]}")
    for author in (record.get("authors") or [])[:8]:
        insts = ", ".join(author.get("institutions") or [])
        _say(f"  author       {author.get('name')}{'  (' + insts + ')' if insts else ''}")
    if record.get("more_authors"):
        _say(f"  ...          {record.get('author_count')} authors in total")
    if record.get("abstract"):
        _say(f"\n  {record['abstract'][:600]}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    from .handlers_core import openalex_resolve

    result = _call(openalex_resolve, {"query": args.query, "entity": args.entity})
    if args.json or _console is None or Table is None:
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        _say(f"[red]{result.get('error')}[/red]")
        return 1
    table = Table(title=f"'{args.query}'  (free)", box=None, padding=(0, 2))
    for column in ("id", "name", "type", "works", "hint"):
        table.add_column(column)
    for match in result.get("matches") or []:
        table.add_row(
            str(match.get("id") or ""),
            str(match.get("name") or "")[:40],
            str(match.get("type") or ""),
            _num(match.get("works_count")),
            str(match.get("hint") or "")[:36],
        )
    _console.print(table)
    return 0


def _print_cost(result: dict[str, Any]) -> None:
    cost = result.get("cost") or {}
    if not cost:
        return
    _say(
        f"\nthis call {_usd(cost.get('this_call_usd'))} "
        f"({cost.get('price_class')})   session {_usd(cost.get('spent_usd'))} "
        f"of {_usd(cost.get('budget_usd'))}"
    )


def cmd_count(args: argparse.Namespace) -> int:
    from .handlers_core import openalex_count

    result = _call(
        openalex_count,
        {
            "entity": args.entity,
            "filter": args.filter,
            "search": args.search,
            "group_by": args.group_by,
        },
    )
    if args.json or _console is None or Table is None:
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        _say(f"[red]{result.get('error')}[/red]")
        if result.get("next_step"):
            _say(f"  {result['next_step']}")
        return 1

    _say(f"[bold]{_num(result.get('total'))}[/bold] {args.entity}")
    groups = result.get("groups") or []
    if groups:
        table = Table(title=result.get("group_by"), box=None, padding=(0, 2), title_justify="left")
        table.add_column("value")
        table.add_column("count", justify="right")
        for row in groups[:20]:
            table.add_row(str(row.get("value"))[:44], _num(row.get("count")))
        _console.print(table)
    if result.get("groups_truncated"):
        _say(f"[yellow]{result['groups_truncated']}[/yellow]")
    _print_cost(result)
    return 0


def cmd_fulltext(args: argparse.Namespace) -> int:
    from .handlers_full import openalex_fulltext

    result = _call(
        openalex_fulltext,
        {
            "id": args.identifier,
            "confirm": bool(args.confirm),
            "format": args.content_format,
        },
    )
    if args.json:
        _say(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        _say(f"[red]{result.get('error')}[/red]")
        if result.get("next_step"):
            _say(f"  {result['next_step']}")
        return 1

    title = str(result.get("title") or "")
    wid = str(result.get("id") or "")
    available = bool(result.get("full_text_available"))
    formats = result.get("available_formats") or []
    header = title if title else wid
    if title and wid:
        header = f"{title}\n{wid}"
    _say(Panel(header, title="fulltext", expand=False) if Panel else header)

    if available:
        _say("  available   [green]true[/green]" if _console else "  available   true")
        _say(f"  formats     {', '.join(formats) if formats else '(none listed)'}")
        if result.get("download_cost_usd") is not None and not args.confirm:
            _say(
                f"  download    {_usd(result.get('download_cost_usd'))} "
                f"only with [bold]--confirm[/bold]"
            )
    else:
        _say("  available   [yellow]false[/yellow]" if _console else "  available   false")
        _say("  formats     (none in OpenAlex)")

    if result.get("oa_url"):
        _say(f"  oa_url      {result['oa_url']}")
    if result.get("pdf_url"):
        _say(f"  pdf_url     {result['pdf_url']}")
    if result.get("source_url"):
        _say(f"  source_url  {result['source_url']}")
    if result.get("characters") is not None:
        _say(f"  characters  {_num(result.get('characters'))}")

    if result.get("note"):
        _say(f"\n  {result['note']}")
    if result.get("next_step"):
        _say(f"\n  {result['next_step']}")

    text = result.get("full_text")
    if text:
        preview = text if len(text) <= 1200 else text[:1200] + "\n…"
        _say("\n" + preview)

    _print_cost(result)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .handlers_core import openalex_search

    result = _call(
        openalex_search,
        {
            "entity": args.entity,
            "search": args.query,
            "search_field": args.search_field,
            "filter": args.filter,
            "sort": args.sort,
            "per_page": args.limit,
        },
    )
    if args.json or _console is None or Table is None:
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        _say(f"[red]{result.get('error')}[/red]")
        if result.get("next_step"):
            _say(f"  {result['next_step']}")
        return 1

    _say(f"[bold]{_num(result.get('total'))}[/bold] matches, showing {result.get('returned')}")
    table = Table(box=None, padding=(0, 2))
    for column in ("year", "cites", "title", "venue"):
        table.add_column(column)
    for row in result.get("results") or []:
        table.add_row(
            str(row.get("year") or ""),
            _num(row.get("cited_by_count")),
            str(row.get("title") or "")[:58],
            str(row.get("venue") or "")[:26],
        )
    _console.print(table)
    _print_cost(result)
    return 0


# --- profile / cache --------------------------------------------------------


def cmd_profile(args: argparse.Namespace) -> int:
    cfg = config_mod.load(refresh=True)
    if not args.value:
        visible = sorted(cfg.visible_tools())
        _say(f"profile: {cfg.profile}")
        _say(f"tools ({len(visible)}): {', '.join(visible)}")
        _say("\nSet with: hermes openalex profile full")
        return 0
    try:
        from hermes_cli.config import set_config_value

        set_config_value("plugins.entries.openalex.profile", args.value, force=True)
    except Exception as exc:
        _say(f"[red]Could not write config: {exc}[/red]")
        return 1
    config_mod.reset()
    updated = config_mod.load(refresh=True)
    _say(f"profile: {updated.profile} ({len(updated.visible_tools())} tools)")
    if args.value == "full" and not updated.budget.allow_text_classification:
        _say(
            "\nopenalex_classify stays hidden until you also allow it, because "
            "it costs $0.01 a call:\n"
            "  hermes config set plugins.entries.openalex.budget.allow_text_classification true"
        )
    _say("\nRestart the gateway if one is running: hermes gateway restart")
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    cfg = config_mod.load(refresh=True)
    client = get_client(cfg)
    if args.action == "clear":
        client.cache.clear()
        _say("Cache cleared.")
        return 0
    stats = client.cache.stats()
    _say(json.dumps(stats, indent=2))
    ledger = tracker.ledger(None).snapshot(cfg.budget.usd_per_session)
    _say(f"\nCache hits saved {_usd(ledger.get('saved_by_cache_usd'))} this session.")
    return 0
