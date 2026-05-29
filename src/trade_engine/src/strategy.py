"""
Strategy interface and concrete stubs.

All trading strategies implement the Strategy protocol. The inference loop and
watcher are designed to program against this interface so that adding a new
strategy type only requires implementing predict() and on_new_candle().

Current concrete strategies (not yet implemented):
  MLStrategy        — wraps predictor.py + registry.py (replaces the existing
                      predictor.infer() call path)
  EmpiricalStrategy — wraps lookup.py (port of the former analysis container)

Stop-loss is per-strategy: set stop_loss_delta to a float (e.g. 0.15 for a 15%
exit threshold) or leave it None to disable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Strategy(Protocol):
    id: str
    stop_loss_delta: float | None  # None = no stop-loss; e.g. 0.15 = exit at 15% loss

    def predict(self, tick: dict) -> float | None:
        """Return predicted probability of YES, or None to skip this tick."""
        ...

    def on_new_candle(self, parquet_path: str) -> None:
        """Called by the watcher when a new raw parquet file arrives.

        ML strategies use this to retrain; empirical strategies use it to
        rebuild the lookup table.
        """
        ...


class MLStrategy:
    """ML model strategy — wraps predictor.py + registry.py.

    Not yet implemented. Will replace the predictor.infer() call in
    inference.py and the train() call in watcher.py.
    """

    def __init__(self, model_cfg: dict) -> None:
        self.id: str = model_cfg["id"]
        self.stop_loss_delta: float | None = model_cfg.get("stop_loss_delta")
        self._cfg = model_cfg

    def predict(self, tick: dict) -> float | None:
        raise NotImplementedError("MLStrategy.predict() not yet wired up")

    def on_new_candle(self, parquet_path: str) -> None:
        raise NotImplementedError("MLStrategy.on_new_candle() not yet wired up")


class EmpiricalStrategy:
    """Empirical lookup table strategy — wraps lookup.py.

    Not yet implemented. Will replace the former analysis container.
    Uses build_table() / lookup() from lookup.py.
    """

    def __init__(self, cfg: dict | None = None) -> None:
        self.id: str = "empirical"
        self.stop_loss_delta: float | None = (cfg or {}).get("stop_loss_delta")

    def predict(self, tick: dict) -> float | None:
        raise NotImplementedError("EmpiricalStrategy.predict() not yet wired up")

    def on_new_candle(self, parquet_path: str) -> None:
        raise NotImplementedError("EmpiricalStrategy.on_new_candle() not yet wired up")
