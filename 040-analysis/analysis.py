import os
import pathlib
import json
import itertools
import sqlalchemy
import dotenv
import pandas
import numpy
import matplotlib.pyplot as plt

#load route/stop config
with open(pathlib.Path(__file__).parent / "routes.json") as f:
    ROUTE_CONFIG = json.load(f)

ROUTE_STOPS = ROUTE_CONFIG["routes"]

STOP_GROUPS = {}
for route, stops in ROUTE_STOPS.items():
    STOP_GROUPS.setdefault(tuple(stops), []).append(route)

#connect to database
dotenv.load_dotenv(pathlib.Path(__file__).parent / ".env")

dbURL = sqlalchemy.engine.URL.create(
    "mysql+pymysql",
    username=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    host=os.environ["DB_HOST"],
)

engine = sqlalchemy.create_engine(dbURL)

#fetch data from db
df = pandas.read_sql("""
    SELECT fetched_at_utc, stop_code, route_code, veh_code, eta_minutes
    FROM collector.eta_observations
    WHERE active = 1
""", engine, parse_dates=["fetched_at_utc"])

#infer hours, day of the week and day-type the date column
df["fetched_at_utc"] = df["fetched_at_utc"].dt.tz_localize("UTC")
df["fetched_at_local"] = df["fetched_at_utc"].dt.tz_convert("Europe/Athens")
df["hour"] = df["fetched_at_local"].dt.hour
df["dow"] = df["fetched_at_local"].dt.dayofweek

TYPES = {0: "Weekday", 1:"Weekday", 2:"Weekday", 3:"Weekday", 4:"Weekday", 5:"Weekend", 6:"Weekend"}
df["day_type"] = df["dow"].map(TYPES)


#group data into episodes

KEY = ["route_code", "veh_code", "stop_code"]
GAP_MAX  = 20
JUMP_MAX = 3

df = df.sort_values(KEY + ["fetched_at_utc"]).reset_index(drop=True)

g = df.groupby(KEY, sort=False)

gap_min = g["fetched_at_utc"].diff().dt.total_seconds() / 60
jump    = g["eta_minutes"].diff()

new_episode = gap_min.isna() | (gap_min > GAP_MAX) | (jump > JUMP_MAX)
df["episode"] = new_episode.cumsum()


#finding ground truth per episode and calculating error

df["eta_min_ep"] = df.groupby("episode")["eta_minutes"].transform("min")

first_min = (df[df["eta_minutes"] == df["eta_min_ep"]].groupby("episode").last())

actual = first_min["fetched_at_utc"] + pandas.to_timedelta(first_min["eta_minutes"], unit="m")

df["actual_arrival"] = df["episode"].map(actual)


df["pred_arrival"] = df["fetched_at_utc"] + pandas.to_timedelta(df["eta_minutes"], unit="m")

#error
df["overrun_signed"] = (df["actual_arrival"] - df["pred_arrival"]).dt.total_seconds() / 60
df["overrun_abs"] = df["overrun_signed"].abs()

#filter out episodes with an episode eta min of more than 2
usable_data = (df["eta_min_ep"] <= 2) & (df["eta_minutes"] >= 3)

OUT = pathlib.Path("output"); OUT.mkdir(exist_ok=True)


ROUTE = ROUTE_CONFIG["distanceAnalysisRoute"]
NEAR_LO, NEAR_HI = 5, 10
MIN_N = 100

cyc = df.copy()
cyc = cyc[cyc["route_code"].astype(str) == ROUTE]
cyc["stop_code"] = cyc["stop_code"].astype(str)

wide = cyc.pivot_table(index=["veh_code", "fetched_at_local", "hour"],columns="stop_code", values="eta_minutes",aggfunc="first").reset_index()

rows = []
for a, b in itertools.pairwise(ROUTE_STOPS[ROUTE]):
    a, b = str(a), str(b)
    if a not in wide.columns or b not in wide.columns:
        continue
    both = wide[wide[a].between(NEAR_LO, NEAR_HI) & wide[b].notna()]
    rows.append(pandas.DataFrame({
        "segment": f"{a} - {b}",
        "hour": both["hour"],
        "implied_min": both[b] - both[a],
    }))

implied = pandas.concat(rows, ignore_index=True)

by_hour = (implied.groupby(["segment", "hour"])["implied_min"].agg(estimate="median", n="size").reset_index())
by_hour = by_hour[by_hour["n"] >= MIN_N]

order = (by_hour.groupby("segment")["estimate"].median().sort_values(ascending=False).index)

fig, ax = plt.subplots(figsize=(9.5, 5.5))

for seg in order:
    d = by_hour[by_hour["segment"] == seg].sort_values("hour")
    ax.plot(d["hour"], d["estimate"], marker="o", ms=4, label=seg)

ax.set_xticks(range(0, 24, 2))
ax.set_xlim(-0.5, 23.5)
ax.set_ylim(bottom=0)
ax.set_xlabel("Hour of day")
ax.set_ylabel("Travel time the model expects (minutes)")
ax.set_title("Predicted Bus Speed Related To Hour")
ax.legend(title="segment", fontsize="small", title_fontsize="small")
ax.grid(axis="y", alpha=0.25)

fig.savefig(OUT / "model_segment_estimate_by_hour.png", dpi=150, bbox_inches="tight")
plt.close(fig)


#signed overrun bands: one minute wide, each integer from -12 to 12 gets its own bar
EDGES  = [-numpy.inf] + [i - 0.5 for i in range(-12, 14)] + [numpy.inf]
LABELS = ["<-12"] + [str(i) for i in range(-12, 13)] + [">12"]
df["signed_band"] = pandas.cut(df["overrun_signed"], bins=EDGES, labels=LABELS)

signed_counts = (df[usable_data]["signed_band"].value_counts().reindex(LABELS, fill_value=0))

fig, ax = plt.subplots(figsize=(9, 5))
ax.bar(signed_counts.index, signed_counts.values, color="mediumpurple")
ax.set_xlabel("Signed Overrun Band (minutes)")
ax.set_ylabel("Count")
ax.set_title("Signed Overrun Distribution - All ETA's")
fig.savefig(OUT / "overrun_signed_bands.png")
plt.close(fig)


#same signed overrun band chart, split out per eta band
ETA_BANDS = ["3-7", "8-15", "16-25", "26-40", "41+"]
ETA_EDGES = [2, 7, 15, 25, 40, 999]
df["eta_band"] = pandas.cut(df["eta_minutes"], bins=ETA_EDGES, labels=ETA_BANDS)

for b in ETA_BANDS:
    sub_counts = (df[usable_data & (df["eta_band"] == b)]["signed_band"].value_counts().reindex(LABELS, fill_value=0))

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(sub_counts.index, sub_counts.values, color="mediumpurple")
    ax.set_xlabel("Signed Overrun Band (minutes)")
    ax.set_ylabel("Count")
    ax.set_title(f"Signed Overrun Distribution — ETA {b}")
    fig.savefig(OUT / f"overrun_signed_bands_eta_{b}.png")
    plt.close(fig)


#accuracy within 1 minute, by stop, eta band 3-7 only, one chart per stop group
for stop_order, routes in STOP_GROUPS.items():
    stop_sub = df[usable_data & (df["eta_band"] == "3-7") & df["route_code"].isin(routes)]
    stop_acc = stop_sub.groupby("stop_code")["overrun_abs"].apply(lambda s: (s <= 1).mean() * 100)
    stop_acc = stop_acc.reindex([s for s in stop_order if s in stop_acc.index])

    label = "/".join(routes)
    fig, ax = plt.subplots(figsize=(max(9, len(stop_acc) * 1.2), 5))
    ax.bar(stop_acc.index.astype(str), stop_acc.values, color="mediumpurple")
    ax.set_ylim(0, 100)
    ax.set_xlabel("Stop Code")
    ax.set_ylabel("% Within 1 Minute")
    ax.set_title(f"Accuracy Within 1 Minute by Stop — Route {label} — ETA 3-7")
    ax.set_xticks(range(len(stop_acc)))
    ax.set_xticklabels(stop_acc.index.astype(str), rotation=45, ha="right")
    fig.savefig(OUT / f"accuracy_within_1min_by_stop_eta_3-7_route_{label.replace('/', '-')}.png")
    plt.close(fig)


#accuracy within 1 minute, by stop, uncontrolled, one chart per stop group
for stop_order, routes in STOP_GROUPS.items():
    stop_sub = df[usable_data & df["route_code"].isin(routes)]
    stop_acc = stop_sub.groupby("stop_code")["overrun_abs"].apply(lambda s: (s <= 1).mean() * 100)
    stop_acc = stop_acc.reindex([s for s in stop_order if s in stop_acc.index])

    label = "/".join(routes)
    fig, ax = plt.subplots(figsize=(max(9, len(stop_acc) * 1.2), 5))
    ax.bar(stop_acc.index.astype(str), stop_acc.values, color="mediumpurple")
    ax.set_ylim(0, 100)
    ax.set_xlabel("Stop Code")
    ax.set_ylabel("% Within 1 Minute")
    ax.set_title(f"Accuracy Within 1 Minute by Stop — Route {label}")
    ax.set_xticks(range(len(stop_acc)))
    ax.set_xticklabels(stop_acc.index.astype(str), rotation=45, ha="right")
    fig.savefig(OUT / f"accuracy_within_1min_by_stop_route_{label.replace('/', '-')}.png")
    plt.close(fig)


#accuracy within 1 minute, by hour of day
hour_acc = df[usable_data].groupby("hour")["overrun_abs"].apply(lambda s: (s <= 1).mean() * 100).reindex(range(24))

fig, ax = plt.subplots(figsize=(9, 5))
ax.plot(hour_acc.index, hour_acc.values, color="mediumpurple", marker="o")
ax.set_ylim(0, 100)
ax.set_xlabel("Hour of Day")
ax.set_ylabel("% Within 1 Minute")
ax.set_title("Accuracy Within 1 Minute by Hour")
ax.set_xticks(range(24))
fig.savefig(OUT / "accuracy_within_1min_by_hour.png")
plt.close(fig)


#accuracy within 1 minute, by hour of day — eta band 3-7 only
hour_sub = df[usable_data & (df["eta_band"] == "3-7")]
hour_acc_eta = hour_sub.groupby("hour")["overrun_abs"].apply(lambda s: (s <= 1).mean() * 100).reindex(range(24))

fig, ax = plt.subplots(figsize=(9, 5))
ax.plot(hour_acc_eta.index, hour_acc_eta.values, color="mediumpurple", marker="o")
ax.set_ylim(0, 100)
ax.set_xlabel("Hour of Day")
ax.set_ylabel("% Within 1 Minute")
ax.set_title("Accuracy Within 1 Minute by Hour — ETA 3-7")
ax.set_xticks(range(24))
fig.savefig(OUT / "accuracy_within_1min_by_hour_eta_3-7.png")
plt.close(fig)