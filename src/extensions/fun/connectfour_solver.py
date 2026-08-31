"""Deterministic Connect Four search used by the hard-mode bot.

The public API accepts Fishie's existing 6x7, top-to-bottom board format.  The
search uses a compact bitboard internally so deeper searches do not block the
Discord event loop for as long as the older list-based minimax did.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

ROWS = 6
COLS = 7
STRIDE = ROWS + 1
WIN_SCORE = 1_000_000
INFINITY = WIN_SCORE + 100_000

Board = list[list[str | None]]

_CENTER_ORDER = (3, 2, 4, 1, 5, 0, 6)
_CENTER_RANK = {column: rank for rank, column in enumerate(_CENTER_ORDER)}
_BOTTOM_MASKS = tuple(1 << (column * STRIDE) for column in range(COLS))
_COLUMN_MASKS = tuple(((1 << ROWS) - 1) << (column * STRIDE) for column in range(COLS))
_TOP_MASKS = tuple(1 << (column * STRIDE + ROWS - 1) for column in range(COLS))
_BOARD_MASK = sum(_COLUMN_MASKS)


def _make_windows() -> tuple[int, ...]:
    windows: list[int] = []
    for row in range(ROWS):
        for column in range(COLS - 3):
            windows.append(
                sum(1 << ((column + offset) * STRIDE + row) for offset in range(4))
            )
    for row in range(ROWS - 3):
        for column in range(COLS):
            windows.append(
                sum(1 << (column * STRIDE + row + offset) for offset in range(4))
            )
    for row in range(ROWS - 3):
        for column in range(COLS - 3):
            windows.append(
                sum(
                    1 << ((column + offset) * STRIDE + row + offset)
                    for offset in range(4)
                )
            )
    for row in range(3, ROWS):
        for column in range(COLS - 3):
            windows.append(
                sum(
                    1 << ((column + offset) * STRIDE + row - offset)
                    for offset in range(4)
                )
            )
    return tuple(windows)


_WINDOWS = _make_windows()


@dataclass(frozen=True, slots=True)
class ScoredMove:
    """A legal move and its score from the requested AI's perspective."""

    column: int
    score: int
    depth: int
    tactical: Literal["win", "block", "forced_loss"] | None = None


class _Bound(IntEnum):
    EXACT = 0
    LOWER = 1
    UPPER = 2


@dataclass(slots=True)
class _TranspositionEntry:
    depth: int
    score: int
    bound: _Bound
    best_column: int | None


class _SearchTimeout(Exception):
    pass


def _has_won(bits: int) -> bool:
    for shift in (1, STRIDE, STRIDE - 1, STRIDE + 1):
        connected = bits & (bits >> shift)
        if connected & (connected >> (2 * shift)):
            return True
    return False


def _move_bit(mask: int, column: int) -> int:
    return (mask + _BOTTOM_MASKS[column]) & _COLUMN_MASKS[column]


def _legal_columns(mask: int) -> tuple[int, ...]:
    return tuple(column for column in _CENTER_ORDER if not mask & _TOP_MASKS[column])


def _winning_columns(bits: int, mask: int) -> tuple[int, ...]:
    winners: list[int] = []
    for column in _legal_columns(mask):
        if _has_won(bits | _move_bit(mask, column)):
            winners.append(column)
    return tuple(winners)


def _convert_board(board: Board, ai_color: str) -> tuple[int, int, int]:
    if ai_color not in {"yellow", "red"}:
        raise ValueError("Unknown Connect Four color.")
    if len(board) != ROWS or any(len(row) != COLS for row in board):
        raise ValueError("Connect Four boards must be 6 rows by 7 columns.")

    ai_bits = 0
    opponent_bits = 0
    for row, values in enumerate(board):
        for column, value in enumerate(values):
            if value not in {None, "yellow", "red"}:
                raise ValueError("Connect Four boards contain an unknown color.")
            if value is None:
                continue
            bit = 1 << (column * STRIDE + (ROWS - 1 - row))
            if value == ai_color:
                ai_bits |= bit
            else:
                opponent_bits |= bit

    mask = ai_bits | opponent_bits
    # A gap below a piece cannot occur in a position reached through legal drops.
    for column in range(COLS):
        occupied = (mask & _COLUMN_MASKS[column]) >> (column * STRIDE)
        if occupied and occupied != (1 << occupied.bit_length()) - 1:
            raise ValueError("Connect Four pieces must be supported by gravity.")
    return ai_bits, opponent_bits, mask


class _Solver:
    def __init__(self, deadline: float | None):
        self.deadline = deadline
        self.nodes = 0
        self.table: dict[tuple[int, int, bool], _TranspositionEntry] = {}

    def _check_time(self) -> None:
        self.nodes += 1
        # Checking periodically is substantially cheaper than calling monotonic
        # for every node while still keeping the latency bound responsive.
        if (
            self.deadline is not None
            and self.nodes & 1023 == 0
            and time.monotonic() >= self.deadline
        ):
            raise _SearchTimeout

    @staticmethod
    def _ordered_columns(mask: int, preferred: int | None = None) -> tuple[int, ...]:
        columns = list(_legal_columns(mask))
        if preferred in columns:
            columns.remove(preferred)
            columns.insert(0, preferred)
        return tuple(columns)

    @staticmethod
    def _threat_value(empty: int, playable: int, *, owner_is_yellow: bool) -> int:
        if empty & playable:
            return 1_200
        bit_index = empty.bit_length() - 1
        level_from_bottom = bit_index % STRIDE
        yellow_controls_parity = level_from_bottom % 2 == 0
        # Odd rows from the bottom favor the opening (yellow) player, while
        # even rows favor red. This is not a proof by itself, so it remains a
        # modest leaf-evaluation bonus rather than overriding searched tactics.
        parity_bonus = 90 if yellow_controls_parity == owner_is_yellow else 20
        return 100 + parity_bonus

    @classmethod
    def _heuristic(
        cls,
        current: int,
        opponent: int,
        mask: int,
        *,
        current_is_yellow: bool,
    ) -> int:
        score = 0
        center_mask = _COLUMN_MASKS[COLS // 2]
        score += (current & center_mask).bit_count() * 18
        score -= (opponent & center_mask).bit_count() * 18

        playable = 0
        for column in _legal_columns(mask):
            playable |= _move_bit(mask, column)

        for window in _WINDOWS:
            own = (current & window).bit_count()
            enemy = (opponent & window).bit_count()
            if own and enemy:
                continue
            empty = window & ~mask
            if own:
                if own == 3:
                    score += cls._threat_value(
                        empty, playable, owner_is_yellow=current_is_yellow
                    )
                elif own == 2:
                    score += 36
                elif own == 1:
                    score += 4
            elif enemy:
                if enemy == 3:
                    score -= cls._threat_value(
                        empty, playable, owner_is_yellow=not current_is_yellow
                    )
                elif enemy == 2:
                    score -= 36
                elif enemy == 1:
                    score -= 4

        own_wins = len(_winning_columns(current, mask))
        enemy_wins = len(_winning_columns(opponent, mask))
        # Two independently playable threats are a fork and deserve a score
        # far above ordinary positional patterns.
        score += own_wins * 2_200 + (8_500 if own_wins > 1 else 0)
        score -= enemy_wins * 2_200 + (8_500 if enemy_wins > 1 else 0)
        return max(-100_000, min(100_000, score))

    def negamax(
        self,
        current: int,
        opponent: int,
        mask: int,
        depth: int,
        alpha: int,
        beta: int,
        ply: int,
        current_is_yellow: bool,
    ) -> int:
        self._check_time()
        if _has_won(opponent):
            return -WIN_SCORE + ply
        if mask == _BOARD_MASK:
            return 0

        immediate = _winning_columns(current, mask)
        if immediate:
            return WIN_SCORE - ply - 1

        opponent_wins = _winning_columns(opponent, mask)
        if len(opponent_wins) > 1:
            return -WIN_SCORE + ply + 2
        if depth <= 0:
            return self._heuristic(
                current,
                opponent,
                mask,
                current_is_yellow=current_is_yellow,
            )

        key = (current, opponent, current_is_yellow)
        entry = self.table.get(key)
        if entry is not None and entry.depth >= depth:
            if entry.bound is _Bound.EXACT:
                return entry.score
            if entry.bound is _Bound.LOWER:
                alpha = max(alpha, entry.score)
            else:
                beta = min(beta, entry.score)
            if alpha >= beta:
                return entry.score

        # Keep the effective window after applying a transposition bound. A
        # cutoff against this narrowed window is still only a bound; treating
        # it as exact can poison later iterative-deepening passes.
        search_alpha = alpha

        forced_column = opponent_wins[0] if opponent_wins else None
        if forced_column is not None:
            columns = (forced_column,)
        else:
            columns = self._ordered_columns(
                mask, entry.best_column if entry is not None else None
            )

        best_score = -INFINITY
        best_column: int | None = None
        cutoff = False
        for column in columns:
            move = _move_bit(mask, column)
            score = -self.negamax(
                opponent,
                current | move,
                mask | move,
                depth - 1,
                -beta,
                -alpha,
                ply + 1,
                not current_is_yellow,
            )
            if score > best_score:
                best_score = score
                best_column = column
            alpha = max(alpha, score)
            if alpha >= beta:
                cutoff = True
                break

        if cutoff:
            bound = _Bound.LOWER
        elif best_score <= search_alpha:
            bound = _Bound.UPPER
        else:
            bound = _Bound.EXACT
        self.table[key] = _TranspositionEntry(depth, best_score, bound, best_column)
        return best_score


def _adaptive_depth(empty_cells: int) -> int:
    if empty_cells <= 12:
        return empty_cells
    if empty_cells <= 18:
        return 10
    if empty_cells <= 28:
        return 9
    return 8


def score_moves(
    board: Board,
    ai_color: str,
    *,
    max_depth: int | None = None,
    time_limit: float | None = 0.6,
) -> tuple[ScoredMove, ...]:
    """Score every legal move, best first, without mutating ``board``.

    ``time_limit=None`` is useful for deterministic offline analysis and tests.
    Live callers should retain the small default latency budget and run this
    CPU-bound function in ``asyncio.to_thread``.
    """

    ai_bits, opponent_bits, mask = _convert_board(board, ai_color)
    columns = _legal_columns(mask)
    if not columns:
        raise ValueError("There are no legal Connect Four moves.")
    if _has_won(ai_bits) or _has_won(opponent_bits):
        raise ValueError("The Connect Four game has already ended.")

    deadline = None if time_limit is None else time.monotonic() + max(0.01, time_limit)
    solver = _Solver(deadline)
    empty_cells = ROWS * COLS - mask.bit_count()
    if max_depth is not None:
        target_depth = max_depth
    elif time_limit is not None:
        # Live hard-mode searches have a fixed deadline.  Let iterative
        # deepening use the whole budget instead of stopping at a shallow
        # heuristic cap while there is still time available.
        target_depth = empty_cells
    else:
        target_depth = _adaptive_depth(empty_cells)
    target_depth = max(1, min(target_depth, empty_cells))

    immediate_wins = set(_winning_columns(ai_bits, mask))
    opponent_wins = set(_winning_columns(opponent_bits, mask))
    completed: dict[int, int] = {}
    completed_depth = 0

    for depth in range(1, target_depth + 1):
        iteration: dict[int, int] = {}
        try:
            for column in columns:
                if column in immediate_wins:
                    iteration[column] = WIN_SCORE - 1
                    continue
                if opponent_wins and column not in opponent_wins:
                    iteration[column] = -WIN_SCORE + 2
                    continue
                move = _move_bit(mask, column)
                iteration[column] = -solver.negamax(
                    opponent_bits,
                    ai_bits | move,
                    mask | move,
                    depth - 1,
                    -INFINITY,
                    INFINITY,
                    1,
                    ai_color != "yellow",
                )
        except _SearchTimeout:
            break
        completed = iteration
        completed_depth = depth
        if immediate_wins or all(
            abs(score) >= WIN_SCORE - 42 for score in iteration.values()
        ):
            break

    if not completed:
        # The first pass is deliberately cheap, but retain a deterministic and
        # tactically safe fallback for an exceptionally tiny scheduling window.
        for column in columns:
            if column in immediate_wins:
                completed[column] = WIN_SCORE - 1
            elif opponent_wins and column not in opponent_wins:
                completed[column] = -WIN_SCORE + 2
            else:
                move = _move_bit(mask, column)
                completed[column] = -_Solver._heuristic(
                    opponent_bits,
                    ai_bits | move,
                    mask | move,
                    current_is_yellow=ai_color != "yellow",
                )

    forced_loss = len(opponent_wins) > 1
    return tuple(
        ScoredMove(
            column,
            score,
            completed_depth,
            (
                "win"
                if column in immediate_wins
                else (
                    "forced_loss"
                    if forced_loss
                    else "block" if opponent_wins and column in opponent_wins else None
                )
            ),
        )
        for column, score in sorted(
            completed.items(),
            key=lambda item: (-item[1], _CENTER_RANK[item[0]]),
        )
    )


def choose_best_move(
    board: Board,
    ai_color: str,
    *,
    max_depth: int | None = None,
    time_limit: float | None = 0.6,
) -> int:
    """Return the deterministic best move; intentional blunders belong upstream."""

    return score_moves(board, ai_color, max_depth=max_depth, time_limit=time_limit)[
        0
    ].column
