import os
import pathlib
import json
import itertools
import sqlalchemy
import dotenv
import pandas
import numpy
import matplotlib.pyplot as plt

BASE_DIR = pathlib.Path(__file__).parent
OUT = pathlib.Path("output")

with open(BASE_DIR / "routes.json") as f:
    ROUTE_CONFIG = json.load(f)

ROUTE_STOPS = ROUTE_CONFIG["routes"]
ROUTE = ROUTE_CONFIG["distanceAnalysisRoute"]

STOP_GROUPS = {}
for route, stops in ROUTE_STOPS.items():
    STOP_GROUPS.setdefault(tuple(stops), []).append(route)

#one minute wide, each integer from -12 to 12 gets its own bar
EDGES  = [-numpy.inf] + [i - 0.5 for i in range(-12, 14)] + [numpy.inf]
LABELS = ["<-12"] + [str(i) for i in range(-12, 13)] + [">12"]

ETA_BANDS = ["3-7", "8-15", "16-25", "26-40", "41+"]
ETA_EDGES = [2, 7, 15, 25, 40, 999]


def make_engine():
    dotenv.load_dotenv(BASE_DIR / ".env")
    dbURL = sqlalchemy.engine.URL.create(
        "mysql+pymysql",
        username=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
    )
    return sqlalchemy.create_engine(dbURL)


def load_observations(engine):
    return pandas.read_sql("""
        SELECT fetched_at_utc, stop_code, route_code, veh_code, eta_minutes, collector_version
        FROM collector.eta_observations
    """, engine, parse_dates=["fetched_at_utc"])


def add_time_columns(df):
    df["fetched_at_utc"] = df["fetched_at_utc"].dt.tz_localize("UTC")
    df["fetched_at_local"] = df["fetched_at_utc"].dt.tz_convert("Europe/Athens")
    df["hour"] = df["fetched_at_local"].dt.hour
    return df


def build_episodes(df, gap_max=20, jump_max=3):
    key = ["route_code", "veh_code", "stop_code"]
    df = df.sort_values(key + ["fetched_at_utc"]).reset_index(drop=True)

    g = df.groupby(key, sort=False)
    gap_min = g["fetched_at_utc"].diff().dt.total_seconds() / 60
    jump = g["eta_minutes"].diff()

    new_episode = gap_min.isna() | (gap_min > gap_max) | (jump > jump_max)
    df["episode"] = new_episode.cumsum()
    return df


def compute_error(df):
    df["eta_min_ep"] = df.groupby("episode")["eta_minutes"].transform("min")

    first_min = df[df["eta_minutes"] == df["eta_min_ep"]].groupby("episode").last()
    actual = first_min["fetched_at_utc"] + pandas.to_timedelta(first_min["eta_minutes"], unit="m")
    df["actual_arrival"] = df["episode"].map(actual)

    df["pred_arrival"] = df["fetched_at_utc"] + pandas.to_timedelta(df["eta_minutes"], unit="m")

    df["overrun_signed"] = (df["actual_arrival"] - df["pred_arrival"]).dt.total_seconds() / 60
    df["overrun_abs"] = df["overrun_signed"].abs()

    df["signed_band"] = pandas.cut(df["overrun_signed"], bins=EDGES, labels=LABELS)
    df["eta_band"] = pandas.cut(df["eta_minutes"], bins=ETA_EDGES, labels=ETA_BANDS)
    return df


def implied_segment_times(df, route, stops, near=(5, 10)):
    near_lo, near_hi = near
    cyc = df[df["route_code"].astype(str) == route].copy()
    cyc["stop_code"] = cyc["stop_code"].astype(str)

    wide = cyc.pivot_table(
        index=["veh_code", "fetched_at_local", "hour"],
        columns="stop_code", values="eta_minutes", aggfunc="first",
    ).reset_index()

    rows = []
    for a, b in itertools.pairwise(stops):
        a, b = str(a), str(b)
        if a not in wide.columns or b not in wide.columns:
            continue
        both = wide[wide[a].between(near_lo, near_hi) & wide[b].notna()]
        rows.append(pandas.DataFrame({
            "segment": f"{a} - {b}",
            "hour": both["hour"],
            "implied_min": both[b] - both[a],
        }))

    return pandas.concat(rows, ignore_index=True)


#--- plots ---

def plot_segment_estimate_by_hour(df, route, stops, min_n=100):
    implied = implied_segment_times(df, route, stops)

    by_hour = (implied.groupby(["segment", "hour"])["implied_min"]
               .agg(estimate="median", n="size").reset_index())
    by_hour = by_hour[by_hour["n"] >= min_n]

    order = by_hour.groupby("segment")["estimate"].median().sort_values(ascending=False).index

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


def plot_signed_bands(df, mask, title_suffix, filename):
    counts = df[mask]["signed_band"].value_counts().reindex(LABELS, fill_value=0)

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(counts.index, counts.values, color="mediumpurple")
    ax.set_xlabel("Signed Overrun Band (minutes)")
    ax.set_ylabel("Count")
    ax.set_title(f"Signed Overrun Distribution — {title_suffix}")
    fig.savefig(OUT / filename)
    plt.close(fig)


def plot_accuracy_by_stop(df, mask, title_suffix, filename_suffix):
    for stop_order, routes in STOP_GROUPS.items():
        stop_sub = df[mask & df["route_code"].isin(routes)]
        stop_acc = stop_sub.groupby("stop_code")["overrun_abs"].apply(lambda s: (s <= 1).mean() * 100)
        stop_acc = stop_acc.reindex([s for s in stop_order if s in stop_acc.index])

        label = "/".join(routes)
        fig, ax = plt.subplots(figsize=(max(9, len(stop_acc) * 1.2), 5))
        ax.bar(stop_acc.index.astype(str), stop_acc.values, color="mediumpurple")
        ax.set_ylim(0, 100)
        ax.set_xlabel("Stop Code")
        ax.set_ylabel("% Within 1 Minute")
        ax.set_title(f"Accuracy Within 1 Minute by Stop — Route {label}{title_suffix}")
        ax.set_xticks(range(len(stop_acc)))
        ax.set_xticklabels(stop_acc.index.astype(str), rotation=45, ha="right")
        fig.savefig(OUT / f"accuracy_within_1min_by_stop{filename_suffix}_route_{label.replace('/', '-')}.png")
        plt.close(fig)


def plot_accuracy_by_hour(df, mask, title_suffix, filename_suffix):
    hour_acc = df[mask].groupby("hour")["overrun_abs"].apply(lambda s: (s <= 1).mean() * 100).reindex(range(24))

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(hour_acc.index, hour_acc.values, color="mediumpurple", marker="o")
    ax.set_ylim(0, 100)
    ax.set_xlabel("Hour of Day")
    ax.set_ylabel("% Within 1 Minute")
    ax.set_title(f"Accuracy Within 1 Minute by Hour{title_suffix}")
    ax.set_xticks(range(24))
    fig.savefig(OUT / f"accuracy_within_1min_by_hour{filename_suffix}.png")
    plt.close(fig)


def main():
    OUT.mkdir(exist_ok=True)

    engine = make_engine()
    df = compute_error(build_episodes(add_time_columns(load_observations(engine))))

    #filter out episodes with an episode eta min of more than 2
    usable_data = (df["eta_min_ep"] <= 2) & (df["eta_minutes"] >= 3)

    plot_segment_estimate_by_hour(df, ROUTE, ROUTE_STOPS[ROUTE])

    plot_signed_bands(df, usable_data, "All ETA's", "overrun_signed_bands.png")
    for b in ETA_BANDS:
        plot_signed_bands(df, usable_data & (df["eta_band"] == b), f"ETA {b}", f"overrun_signed_bands_eta_{b}.png")

    plot_accuracy_by_stop(df, usable_data & (df["eta_band"] == "3-7"), " — ETA 3-7", "_eta_3-7")
    plot_accuracy_by_stop(df, usable_data, "", "")

    plot_accuracy_by_hour(df, usable_data, "", "")
    plot_accuracy_by_hour(df, usable_data & (df["eta_band"] == "3-7"), " — ETA 3-7", "_eta_3-7")


if __name__ == "__main__":
    main()
