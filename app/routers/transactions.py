import json
from fastapi import APIRouter, HTTPException
from app.database import get_pool

router = APIRouter(
	prefix="/transactions",
	tags=["transactions"]
)

# Seasons whose data reports outbid claims (FAILED_INVALIDPLAYERSOURCE). 2018 rows are
# stored but 2018 has no losing-bid status, so it is left out of every outbid stat.
LOSING_BID_SEASONS = list(range(2019, 2027))

# One event time per transaction: processDate exists only on waiver rows.
EVENT_AT = "COALESCE(t.processed_at, t.proposed_at)"
# A waiver run = all claims processed on the same UTC date.
RUN_DATE = f"({EVENT_AT} AT TIME ZONE 'UTC')::date"

# The executed WAIVER claim that won claim t's added player (i) in t's run: same season,
# same added player, same run date. Exposes w.owner, w.team_id, w.bid_amount.
RUN_WINNER = f"""LEFT JOIN LATERAL (
				SELECT wt.owner, wt.team_id, x.bid_amount
				FROM transactions x
				JOIN transaction_items xi ON xi.transaction_id = x.id AND xi.item_type = 'ADD'
				JOIN teams wt             ON wt.team_id = x.team_id
				WHERE x.type = 'WAIVER' AND x.status = 'EXECUTED'
				  AND x.season = t.season
				  AND xi.espn_player_id = i.espn_player_id
				  AND (COALESCE(x.processed_at, x.proposed_at) AT TIME ZONE 'UTC')::date = {RUN_DATE}
				ORDER BY x.bid_amount DESC, x.id
				LIMIT 1
			) w ON TRUE"""


@router.get("/seasons")
async def get_transaction_seasons():
	"""Seasons with transaction data."""
	async with (await get_pool()).acquire() as db:
		rows = await db.fetch("""
			SELECT
				t.season,
				COUNT(*)                                                                       AS transactions,
				COUNT(*) FILTER (WHERE t.type = 'WAIVER' AND t.status = 'EXECUTED')            AS waiver_claims_won,
				COALESCE(SUM(t.bid_amount) FILTER (WHERE t.type = 'WAIVER'
				                                     AND t.status = 'EXECUTED'), 0)            AS faab_spent,
				COUNT(*) FILTER (WHERE t.type = 'FREEAGENT' AND t.status = 'EXECUTED')         AS free_agent_moves,
				MIN(t.week)                                                                    AS first_week,
				MAX(t.week)                                                                    AS last_week,
				array_agg(DISTINCT t.week ORDER BY t.week)                                     AS weeks
			FROM transactions t
			GROUP BY t.season
			ORDER BY t.season
		""")
		return [
			{**dict(row), "has_losing_bids": int(row["season"]) in LOSING_BID_SEASONS}
			for row in rows
		]


@router.get("/season/{year}/week/{week}")
async def get_week_transactions(year: int, week: int):
	"""Every stored transaction for a week, in event-time order, with the players moved."""
	async with (await get_pool()).acquire() as db:
		rows = await db.fetch(f"""
			SELECT
				t.id,
				t.espn_txn_id,
				t.type,
				t.status,
				t.bid_amount,
				t.proposed_at,
				t.processed_at,
				{EVENT_AT}          AS event_at,
				t.cancel_twin_of,
				tm.owner,
				tm.team_id,
				w.owner             AS outbid_by_owner,
				w.bid_amount        AS outbid_by_bid
			FROM transactions t
			JOIN teams tm ON tm.team_id = t.team_id
			-- only outbid claims look up a winner; they always have exactly one ADD
			LEFT JOIN transaction_items i ON i.transaction_id = t.id AND i.item_type = 'ADD'
			                             AND t.status = 'FAILED_INVALIDPLAYERSOURCE'
			{RUN_WINNER}
			WHERE t.season = $1 AND t.week = $2
			ORDER BY {EVENT_AT}, t.id
		""", year, week)
		if not rows:
			raise HTTPException(status_code=404, detail="No transactions for this week")

		items = await db.fetch("""
			SELECT
				i.transaction_id,
				i.item_type,
				i.espn_player_id,
				i.player_name,
				i.position,
				ft.owner AS from_owner,
				tt.owner AS to_owner
			FROM transaction_items i
			LEFT JOIN teams ft ON ft.team_id = i.from_team_id
			LEFT JOIN teams tt ON tt.team_id = i.to_team_id
			WHERE i.transaction_id = ANY($1::int[])
			ORDER BY i.transaction_id, i.item_type, i.espn_player_id
		""", [r["id"] for r in rows])
		by_txn = {}
		for i in items:
			by_txn.setdefault(i["transaction_id"], []).append({
				"item_type":      i["item_type"],
				"espn_player_id": i["espn_player_id"],
				"player_name":    i["player_name"],
				"position":       i["position"],
				"from_owner":     i["from_owner"],
				"to_owner":       i["to_owner"],
			})

		return {
			"season": year,
			"week":   week,
			"transactions": [
				{
					"espn_txn_id":    row["espn_txn_id"],
					"type":           row["type"],
					"status":         row["status"],
					"bid_amount":     row["bid_amount"],
					"proposed_at":    row["proposed_at"],
					"processed_at":   row["processed_at"],
					"event_at":       row["event_at"],
					"cancel_twin_of": row["cancel_twin_of"],
					"owner":          row["owner"],
					"team_id":        row["team_id"],
					"items":          by_txn.get(row["id"], []),
					**({"outbid_by": {"owner": row["outbid_by_owner"], "bid_amount": row["outbid_by_bid"]}}
					   if row["status"] == "FAILED_INVALIDPLAYERSOURCE" else {}),
				}
				for row in rows
			]
		}


@router.get("/team/{team_id}")
async def get_team_transactions(team_id: int):
	"""Per season: FAAB spent, claims won, claims lost (outbid), other failed claims, free agent adds, drops."""
	async with (await get_pool()).acquire() as db:
		team = await db.fetchrow("SELECT owner FROM teams WHERE team_id = $1", team_id)
		if not team:
			raise HTTPException(status_code=404, detail="Team not found")

		rows = await db.fetch("""
			SELECT
				t.season,
				COALESCE(SUM(t.bid_amount) FILTER (WHERE t.type = 'WAIVER'
				                                     AND t.status = 'EXECUTED'), 0)          AS faab_spent,
				COUNT(*) FILTER (WHERE t.type = 'WAIVER' AND t.status = 'EXECUTED')          AS claims_won,
				COUNT(*) FILTER (WHERE t.type = 'WAIVER'
				                   AND t.status = 'FAILED_INVALIDPLAYERSOURCE')              AS claims_lost,
				COUNT(*) FILTER (WHERE t.type = 'WAIVER' AND t.status LIKE 'FAILED%'
				                   AND t.status <> 'FAILED_INVALIDPLAYERSOURCE')             AS claims_failed_other,
				(SELECT COUNT(*) FROM transaction_items i
				   JOIN transactions x ON x.id = i.transaction_id
				  WHERE x.team_id = $1 AND x.season = t.season AND x.status = 'EXECUTED'
				    AND x.type = 'FREEAGENT' AND i.item_type = 'ADD')                        AS free_agent_adds,
				(SELECT COUNT(*) FROM transaction_items i
				   JOIN transactions x ON x.id = i.transaction_id
				  WHERE x.team_id = $1 AND x.season = t.season AND x.status = 'EXECUTED'
				    AND i.item_type = 'DROP')                                                AS drops
			FROM transactions t
			WHERE t.team_id = $1
			GROUP BY t.season
			ORDER BY t.season
		""", team_id)

		return {
			"team_id": team_id,
			"owner":   team["owner"],
			"seasons": [
				{
					**dict(row),
					# outbid claims are only reported for seasons whose data has them
					"claims_lost": row["claims_lost"] if int(row["season"]) in LOSING_BID_SEASONS else None,
				}
				for row in rows
			]
		}


@router.get("/player/{player_name:path}")
async def get_player_transactions(player_name: str):
	"""Every executed add and drop of a player across seasons. Case-insensitive partial match."""
	async with (await get_pool()).acquire() as db:
		rows = await db.fetch(f"""
			SELECT
				t.season,
				t.week,
				{EVENT_AT}       AS event_at,
				t.type,
				i.item_type,
				i.player_name,
				i.position,
				tm.owner,
				tm.team_id,
				CASE WHEN i.item_type = 'ADD' THEN t.bid_amount END AS bid_amount
			FROM transaction_items i
			JOIN transactions t ON t.id = i.transaction_id
			JOIN teams tm       ON tm.team_id = t.team_id
			WHERE i.player_name ILIKE $1
			  AND t.status = 'EXECUTED'
			ORDER BY {EVENT_AT}, t.id, i.item_type
		""", f"%{player_name}%")

		return {
			"query":         player_name,
			"times_added":   sum(1 for r in rows if r["item_type"] == "ADD"),
			"times_dropped": sum(1 for r in rows if r["item_type"] == "DROP"),
			"results":       [dict(row) for row in rows],
		}


@router.get("/records/biggest-bids")
async def get_biggest_bids(limit: int = 25, season: int = None):
	"""Top winning (EXECUTED) WAIVER bids."""
	async with (await get_pool()).acquire() as db:
		rows = await db.fetch(f"""
			SELECT
				t.bid_amount,
				tm.owner,
				tm.team_id,
				i.player_name,
				i.position,
				t.season,
				t.week,
				{EVENT_AT} AS event_at
			FROM transactions t
			JOIN teams tm            ON tm.team_id = t.team_id
			JOIN transaction_items i ON i.transaction_id = t.id AND i.item_type = 'ADD'
			WHERE t.type = 'WAIVER' AND t.status = 'EXECUTED'
			  AND ($2::numeric IS NULL OR t.season = $2)
			ORDER BY t.bid_amount DESC, t.season, t.week, t.espn_txn_id
			LIMIT $1
		""", limit, season)
		return [dict(row) for row in rows]


@router.get("/records/biggest-losing-bids")
async def get_biggest_losing_bids(limit: int = 25, season: int = None):
	"""Top outbid (FAILED_INVALIDPLAYERSOURCE) WAIVER bids, with who won the player in that run.
	Other failures (e.g. FAILED_PLAYERALREADYDROPPED) were not outbid and are left out."""
	async with (await get_pool()).acquire() as db:
		rows = await db.fetch(f"""
			SELECT
				t.bid_amount,
				t.status,
				tm.owner,
				tm.team_id,
				i.player_name,
				i.position,
				t.season,
				t.week,
				{EVENT_AT}  AS event_at,
				w.owner     AS winner_owner,
				w.bid_amount AS winning_bid
			FROM transactions t
			JOIN teams tm            ON tm.team_id = t.team_id
			JOIN transaction_items i ON i.transaction_id = t.id AND i.item_type = 'ADD'
			{RUN_WINNER}
			WHERE t.type = 'WAIVER' AND t.status = 'FAILED_INVALIDPLAYERSOURCE'
			  AND t.season = ANY($2::numeric[])
			  AND ($3::numeric IS NULL OR t.season = $3)
			ORDER BY t.bid_amount DESC, t.season, t.week, t.espn_txn_id
			LIMIT $1
		""", limit, LOSING_BID_SEASONS, season)
		return [dict(row) for row in rows]


@router.get("/records/biggest-overpays")
async def get_biggest_overpays(limit: int = 25, season: int = None):
	"""Winning WAIVER bid minus the highest outbid (FAILED_INVALIDPLAYERSOURCE) bid from a different
	team on the same player in the same run. No competing bid counts as a runner-up of 0."""
	async with (await get_pool()).acquire() as db:
		rows = await db.fetch(f"""
			WITH wins AS (
				SELECT t.id, t.team_id, t.bid_amount, t.season, t.week, i.espn_player_id, i.player_name,
				       i.position, {EVENT_AT} AS event_at, {RUN_DATE} AS run_date, t.espn_txn_id
				FROM transactions t
				JOIN transaction_items i ON i.transaction_id = t.id AND i.item_type = 'ADD'
				WHERE t.type = 'WAIVER' AND t.status = 'EXECUTED'
				  AND t.season = ANY($2::numeric[])
				  AND ($3::numeric IS NULL OR t.season = $3)
			),
			losses AS (
				SELECT t.team_id, t.bid_amount, t.season, i.espn_player_id, {RUN_DATE} AS run_date, t.id
				FROM transactions t
				JOIN transaction_items i ON i.transaction_id = t.id AND i.item_type = 'ADD'
				WHERE t.type = 'WAIVER' AND t.status = 'FAILED_INVALIDPLAYERSOURCE'
				  AND t.season = ANY($2::numeric[])
			)
			SELECT
				w.bid_amount,
				COALESCE(r.bid_amount, 0)                 AS runner_up_bid,
				w.bid_amount - COALESCE(r.bid_amount, 0)  AS overpay,
				wt.owner,
				w.team_id,
				rt.owner                                  AS runner_up_owner,
				w.player_name,
				w.position,
				w.season,
				w.week,
				w.event_at
			FROM wins w
			JOIN teams wt ON wt.team_id = w.team_id
			LEFT JOIN LATERAL (
				SELECT l.team_id, l.bid_amount
				FROM losses l
				WHERE l.season = w.season AND l.espn_player_id = w.espn_player_id
				  AND l.run_date = w.run_date AND l.team_id <> w.team_id
				ORDER BY l.bid_amount DESC, l.id
				LIMIT 1
			) r ON TRUE
			LEFT JOIN teams rt ON rt.team_id = r.team_id
			ORDER BY overpay DESC, w.bid_amount DESC, w.season, w.week, w.espn_txn_id
			LIMIT $1
		""", limit, LOSING_BID_SEASONS, season)
		return [dict(row) for row in rows]


_BARGAIN_SORTS = {
	"ppg":        "ppg DESC, s.points DESC, p.season, p.week, p.id",
	"points":     "s.points DESC, ppg DESC, p.season, p.week, p.id",
	"per_dollar": "points_per_dollar DESC, s.points DESC, p.season, p.week, p.id",
}


@router.get("/records/bargains")
async def get_bargains(paid: bool = True, min_games: int = 3, sort: str = "ppg",
                       season: int = None, limit: int = 25):
	"""Best pickups: EXECUTED WAIVER or FREEAGENT adds, paid (bid > 0) or free (bid = 0).

	Stint: from the pickup's week to the end of that season, or to the week before the
	same team's next pickup of the same player. Games: that team's box_scores rows for the
	player within the stint, starters and bench alike, playoff weeks included. Weeks with
	no NFL game for the player (has_stats = FALSE: NFL bye or no NFL team) are not counted;
	inactive/IR weeks (a zero stat line) are.
	"""
	if sort not in _BARGAIN_SORTS:
		raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_BARGAIN_SORTS)}")
	if sort == "per_dollar" and not paid:
		raise HTTPException(status_code=400, detail="sort=per_dollar needs paid=true")
	async with (await get_pool()).acquire() as db:
		rows = await db.fetch(f"""
			WITH pickups AS (
				SELECT t.id, t.season, t.week, t.team_id, t.type, t.bid_amount,
				       i.espn_player_id, i.player_name, i.position,
				       {EVENT_AT} AS event_at,
				       LEAD(t.week) OVER (PARTITION BY t.season, t.team_id, i.espn_player_id
				                          ORDER BY {EVENT_AT}, t.id) AS next_pickup_week
				FROM transactions t
				JOIN transaction_items i ON i.transaction_id = t.id AND i.item_type = 'ADD'
				WHERE t.status = 'EXECUTED' AND t.type IN ('WAIVER', 'FREEAGENT')
			),
			stints AS (
				SELECT p.id,
				       COUNT(b.id)                       AS games,
				       COALESCE(SUM(b.points_scored), 0) AS points
				FROM pickups p
				LEFT JOIN box_scores b
				       ON b.team_id = p.team_id
				      AND b.season = p.season
				      AND b.espn_player_id = p.espn_player_id
				      AND b.week >= p.week
				      AND (p.next_pickup_week IS NULL OR b.week < p.next_pickup_week)
				      AND b.has_stats
				GROUP BY p.id
			)
			SELECT
				p.season,
				p.week,
				tm.owner,
				tm.team_id,
				p.player_name,
				p.position,
				p.type,
				p.bid_amount,
				p.event_at,
				s.games,
				s.points,
				ROUND(s.points / s.games, 2)                                          AS ppg,
				CASE WHEN p.bid_amount > 0 THEN ROUND(s.points / p.bid_amount, 2) END AS points_per_dollar
			FROM pickups p
			JOIN stints s ON s.id = p.id
			JOIN teams tm ON tm.team_id = p.team_id
			WHERE (CASE WHEN $1 THEN p.bid_amount > 0 ELSE p.bid_amount = 0 END)
			  AND s.games >= GREATEST($2, 1)
			  AND ($3::numeric IS NULL OR p.season = $3)
			ORDER BY {_BARGAIN_SORTS[sort]}
			LIMIT $4
		""", paid, min_games, season, limit)
		return [
			{
				**dict(row),
				"points":            float(row["points"]),
				"ppg":               float(row["ppg"]),
				"points_per_dollar": float(row["points_per_dollar"]) if row["points_per_dollar"] is not None else None,
			}
			for row in rows
		]


@router.get("/records/most-contested")
async def get_most_contested(limit: int = 25, season: int = None):
	"""Players claimed by the most distinct teams in a single waiver run (cancelled claims excluded)."""
	async with (await get_pool()).acquire() as db:
		rows = await db.fetch(f"""
			WITH claims AS (
				SELECT t.season, t.week, {RUN_DATE} AS run_date, i.espn_player_id, i.player_name, i.position,
				       t.team_id, tm.owner, t.bid_amount, t.status, t.id
				FROM transactions t
				JOIN transaction_items i ON i.transaction_id = t.id AND i.item_type = 'ADD'
				JOIN teams tm            ON tm.team_id = t.team_id
				WHERE t.type = 'WAIVER' AND t.status <> 'CANCELED'
				  AND t.season = ANY($2::numeric[])
				  AND ($3::numeric IS NULL OR t.season = $3)
			)
			SELECT
				c.season,
				MIN(c.week)                                                   AS week,
				c.run_date,
				c.espn_player_id,
				MIN(c.player_name)                                            AS player_name,
				MIN(c.position)                                               AS position,
				COUNT(DISTINCT c.team_id)                                     AS teams,
				COUNT(*)                                                      AS claims,
				MAX(c.bid_amount) FILTER (WHERE c.status = 'EXECUTED')        AS winning_bid,
				MIN(c.owner) FILTER (WHERE c.status = 'EXECUTED')             AS winner_owner,
				json_agg(json_build_object('owner', c.owner, 'bid_amount', c.bid_amount, 'status', c.status)
				         ORDER BY c.bid_amount DESC, c.owner, c.id)::text     AS bids
			FROM claims c
			GROUP BY c.season, c.run_date, c.espn_player_id
			ORDER BY teams DESC, claims DESC, winning_bid DESC NULLS LAST, c.season, c.run_date, c.espn_player_id
			LIMIT $1
		""", limit, LOSING_BID_SEASONS, season)
		return [{**dict(row), "bids": json.loads(row["bids"])} for row in rows]
