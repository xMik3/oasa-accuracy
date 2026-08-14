import os
import pathlib
import json
import sqlalchemy
import dotenv
import pandas
import numpy
import matplotlib.pyplot as plt

#load route/stop config
with open(pathlib.Path(__file__).parent / "routes.json") as f:
    ROUTE_STOPS = json.load(f)

#group routes that share the exact same stop sequence (e.g. 3922 and 3924) so
#they get one combined chart, keyed by their stop order
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
    SELECT fetched_at_utc, stop_code, route_code, veh_code, eta_minutes, active
    FROM collector.eta_observations
""", engine, parse_dates=["fetched_at_utc"])

#infer hours, day of the week and day-type the date column
df["fetched_at_utc"] = df["fetched_at_utc"].dt.tz_localize("UTC")
df["fetched_at_local"] = df["fetched_at_utc"].dt.tz_convert("Europe/Athens")
df["hour"] = df["fetched_at_local"].dt.hour
df["dow"] = df["fetched_at_local"].dt.dayofweek

TYPES = {0: "Weekday", 1:"Weekday", 2:"Weekday", 3:"Weekday", 4:"Weekday", 5:"Weekend", 6:"Weekend"}
df["day_type"] = df["dow"].map(TYPES)


#split data into episodes
#we need gap and jump to split the data into episodes:
#gap : real time minutes since the same was listed on the stop
#jump : the difference in prediction time that the same bus gets to the stop

KEY = ["route_code", "veh_code", "stop_code"]
GAP_MAX  = 20
JUMP_MAX = 3

df = df.sort_values(KEY + ["fetched_at_utc"]).reset_index(drop=True)

g = df.groupby(KEY, sort=False)

gap_min = g["fetched_at_utc"].diff().dt.total_seconds() / 60
jump    = g["eta_minutes"].diff()

new_episode = gap_min.isna() | (gap_min > GAP_MAX) | (jump > JUMP_MAX)
df["episode"] = new_episode.cumsum()


#Finding ground truth per episode and calculating error

#ground truth per episode
df["eta_min_ep"] = df.groupby("episode")["eta_minutes"].transform("min")

first_min = (df[df["eta_minutes"] == df["eta_min_ep"]].groupby("episode").last())

actual = first_min["fetched_at_utc"] + pandas.to_timedelta(first_min["eta_minutes"], unit="m")

df["actual_arrival"] = df["episode"].map(actual)

#predicted arrival (in time)
df["pred_arrival"] = df["fetched_at_utc"] + pandas.to_timedelta(df["eta_minutes"], unit="m")

#error
#signed overrun: positive = bus arrived later than predicted, negative = earlier
df["overrun_signed"] = (df["actual_arrival"] - df["pred_arrival"]).dt.total_seconds() / 60
df["overrun_abs"] = df["overrun_signed"].abs()

#filter out episodes with an episode eta min of more than
usable_data = (df["eta_min_ep"] <= 2) & (df["eta_minutes"] >= 3) & (df["active"] == 1)


###
###
###

OUT = pathlib.Path("output"); OUT.mkdir(exist_ok=True)

#signed overrun bands: one minute wide, each integer from -12 to 12 gets its own bar
EDGES  = [-numpy.inf] + [i - 0.5 for i in range(-12, 14)] + [numpy.inf]
LABELS = ["<-12"] + [str(i) for i in range(-12, 13)] + [">12"]
df["signed_band"] = pandas.cut(df["overrun_signed"], bins=EDGES, labels=LABELS)

signed_counts = (df[usable_data]["signed_band"].value_counts().reindex(LABELS, fill_value=0))

fig, ax = plt.subplots(figsize=(9, 5))
ax.bar(signed_counts.index, signed_counts.values, color="mediumpurple")
ax.set_xlabel("Signed Overrun Band (minutes)")
ax.set_ylabel("Count")
ax.set_title("Signed Overrun Distribution")
fig.savefig(OUT / "overrun_signed_bands.png")
plt.close(fig)

###

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

###

#accuracy within 1 minute, by stop — eta band 3-7 only, one chart per stop group
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

###

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

###

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

###