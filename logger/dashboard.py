"""
logger/dashboard.py
Live Rich terminal dashboard with actual failure reasons.
"""

import asyncio
import time

from loguru import logger
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from config import LOG_FILE, TRADE_LOG_FILE

console = Console()


def setup_logging():
    import os
    import sys

    os.makedirs("logs", exist_ok=True)
    logger.remove()

    logger.add(
        LOG_FILE,
        level="INFO",                     # was DEBUG — spam was filling logs
        rotation="20 MB",                 # rotate sooner; keep recent logs handy
        retention=5,                      # keep only 5 rotations (~100 MB ceiling)
        compression="gz",                 # gzip rotated logs
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
        enqueue=True,                     # async-safe writes (avoid race on rotation)
    )

    logger.add(
        TRADE_LOG_FILE,
        level="SUCCESS",
        filter=lambda r: "BUY" in r["message"] or "SELL" in r["message"] or "POSITION" in r["message"],
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )

    logger.add(
        sys.stdout,
        level="INFO",
        colorize=True,
        format="<dim>{time:HH:mm:ss}</dim> | <level>{level:<8}</level> | {message}",
    )


class Dashboard:
    def __init__(self, risk_manager, signal_scorer):
        self.risk_manager   = risk_manager
        self.signal_scorer  = signal_scorer
        self.recent_signals = []
        self.start_time     = time.time()
        self._live: Live | None = None

    def record_signal(self, token: dict):
        self.recent_signals.insert(0, token)
        self.recent_signals = self.recent_signals[:20]

    async def run(self):
        with Live(
            self._render(),
            console=console,
            refresh_per_second=1,
            screen=False,
        ) as live:
            self._live = live
            while True:
                live.update(self._render())
                await asyncio.sleep(1)

    def _render(self):
        layout = Layout()
        layout.split_column(
            Layout(name="header",    size=3),
            Layout(name="main",      ratio=1),
            Layout(name="footer",    size=3),
        )
        layout["main"].split_row(
            Layout(name="positions", ratio=2),
            Layout(name="signals",   ratio=3),
        )
        layout["header"].update(self._render_header())
        layout["positions"].update(self._render_positions())
        layout["signals"].update(self._render_signals())
        layout["footer"].update(self._render_stats())
        return layout

    def _render_header(self):
        uptime_s = int(time.time() - self.start_time)
        h, m, s = uptime_s // 3600, (uptime_s % 3600) // 60, uptime_s % 60
        stats = self.risk_manager.get_stats()
        pnl = stats["total_pnl_sol"]
        pnl_color = "green" if pnl >= 0 else "red"
        sign = "+" if pnl >= 0 else ""

        title = (
            f"[bold cyan]🤖 PUMP BOT[/bold cyan]  |  "
            f"Uptime: {h:02d}:{m:02d}:{s:02d}  |  "
            f"PnL: [{pnl_color}]{sign}{pnl:.4f} SOL[/{pnl_color}]  |  "
            f"Trades: {stats['closed_trades']}  |  "
            f"Win Rate: {stats['win_rate']:.0%}"
        )
        status = "[red]🚨 EMERGENCY STOP[/red]" if stats["emergency_stop"] else "[green]● RUNNING[/green]"
        return Panel(f"{title}  |  {status}", style="bold")

    def _render_positions(self):
        table = Table(
            title="Open Positions",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Symbol",  width=8)
        table.add_column("SOL In",  width=7)
        table.add_column("PnL%",    width=8)
        table.add_column("Age",     width=6)
        table.add_column("Score",   width=5)

        for mint, pos in self.risk_manager.positions.items():
            pnl = pos.pnl_pct
            color = "green" if pnl >= 0 else "red"
            sign  = "+" if pnl >= 0 else ""
            table.add_row(
                pos.symbol,
                f"{pos.sol_invested:.3f}",
                f"[{color}]{sign}{pnl:.1f}%[/{color}]",
                f"{pos.age_minutes:.0f}m",
                str(pos.score),
            )

        if not self.risk_manager.positions:
            table.add_row("[dim]No open positions[/dim]", "", "", "", "")

        return Panel(table, border_style="magenta")

    def _render_signals(self):
        table = Table(
            title="Recent Signals",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold yellow",
        )
        table.add_column("Symbol",   width=8)
        table.add_column("Score",    width=6)
        table.add_column("MC SOL",   width=8)
        table.add_column("Init Buy", width=8)
        table.add_column("Result",   width=14)

        for token in self.recent_signals[:12]:
            score  = token.get("score", 0)

            if token.get("queued_for_buy"):
                result = "[green]BUY SENT[/green]"
            elif token.get("reject_reason"):
                result = f"[red]{str(token['reject_reason'])[:12]}[/red]"
            else:
                result = "[dim]pending[/dim]"

            s_color = "green" if score >= 65 else "yellow" if score >= 40 else "red"

            table.add_row(
                token.get("symbol", "???")[:8],
                f"[{s_color}]{score}[/{s_color}]",
                f"{token.get('market_cap_sol', 0):.1f}",
                f"{token.get('initial_buy_sol', 0):.2f}",
                result,
            )

        return Panel(table, border_style="yellow")

    def _render_stats(self):
        stats = self.risk_manager.get_stats()
        return Panel(
            f"[cyan]Exposure:[/cyan] {stats['total_exposure']:.3f} SOL  |  "
            f"[cyan]Scored:[/cyan] {self.signal_scorer.scored_count}  |  "
            f"[cyan]Open:[/cyan] {stats['open_positions']}  |  "
            f"[cyan]Closed:[/cyan] {stats['closed_trades']}  |  "
            f"[cyan]Log:[/cyan] {LOG_FILE}",
            style="dim",
        )
