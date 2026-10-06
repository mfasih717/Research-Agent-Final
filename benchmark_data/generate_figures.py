import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

os.makedirs("figures", exist_ok=True)

# Academic Color Palette
COLOR_NAVY = "#1B365D"       # Deep Navy
COLOR_TEAL = "#0E8388"       # Rich Teal
COLOR_SLATE = "#334155"      # Dark Slate
COLOR_LIGHT_SLATE = "#64748B"# Slate Blue

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "DejaVu Sans", "Helvetica"],
    "font.size": 10,
    "axes.labelsize": 10.5,
    "axes.titlesize": 11,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 9.5,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linestyle": "--",
    "axes.axisbelow": True
})

# ==============================================================================
# Figure 4.9: Empirical Execution and Answer Accuracy Across Categories (C01–C09)
# ==============================================================================
categories = [
    ("C01", "Direct\nRetrieval", 100.0, 100.0),
    ("C02", "Dept\nRelation", 100.0, 100.0),
    ("C03", "Desg\nRelation", 100.0, 100.0),
    ("C04", "Multi\nRelations", 100.0, 100.0),
    ("C05", "Aggregation", 100.0, 100.0),
    ("C06", "Grouping", 86.67, 86.67),
    ("C07", "Ranking", 93.33, 93.33),
    ("C08", "Date\nFilter", 93.33, 93.33),
    ("C09", "Multi\nConditions", 100.0, 100.0),
]

cat_codes = [c[0] for c in categories]
cat_labels = [c[1] for c in categories]
exec_acc = [c[2] for c in categories]
ans_acc = [c[3] for c in categories]

fig, ax = plt.subplots(figsize=(10.5, 5.0), dpi=300)

x = np.arange(len(cat_codes))
bar_width = 0.36

rects1 = ax.bar(x - bar_width/2, exec_acc, bar_width, label="Execution Accuracy (%)", color=COLOR_NAVY, zorder=3)
rects2 = ax.bar(x + bar_width/2, ans_acc, bar_width, label="Answer Accuracy (%)", color=COLOR_TEAL, zorder=3)

ax.set_ylabel("Accuracy (%)", fontweight="bold", labelpad=8)
ax.set_title("Empirical Execution and Answer Accuracy Across Designed Query Categories (C01–C09)", fontweight="bold", pad=12, fontsize=11)
ax.set_xticks(x)
ax.set_xticklabels(cat_labels)
ax.set_ylim(0, 118)
ax.set_yticks(np.arange(0, 121, 20))
ax.legend(loc="upper right", framealpha=0.95, edgecolor="#CBD5E1")

# Category boundary reference line at 90%
ax.axhline(90, color="#94A3B8", linestyle=":", linewidth=1.2, alpha=0.75, zorder=2)
ax.text(0.05, 91, "Target Accuracy Benchmark (90%)", color="#64748B", fontsize=8, fontstyle="italic")

def format_val(val):
    if abs(val - round(val)) < 0.05:
        return f"{int(round(val))}%"
    return f"{val:.1f}%"

# Label bars with clean formatting and no collision
for rect in rects1:
    h = rect.get_height()
    ax.annotate(format_val(h),
                xy=(rect.get_x() + rect.get_width() / 2, h),
                xytext=(0, 3.5),
                textcoords="offset points",
                ha="center", va="bottom",
                fontsize=7.8, fontweight="bold",
                color=COLOR_SLATE)

for rect in rects2:
    h = rect.get_height()
    ax.annotate(format_val(h),
                xy=(rect.get_x() + rect.get_width() / 2, h),
                xytext=(0, 3.5),
                textcoords="offset points",
                ha="center", va="bottom",
                fontsize=7.8, fontweight="bold",
                color=COLOR_SLATE)

fig.tight_layout()
fig4_9_path = os.path.join("figures", "figure_4_9_category_accuracy.png")
fig.savefig(fig4_9_path, dpi=300, bbox_inches="tight")
plt.close(fig)
print(f"Generated: {fig4_9_path}")


# ==============================================================================
# Figure 4.10: Multilingual Performance Distribution Across Urdu, Roman Urdu, English
# ==============================================================================
lang_labels = ["Urdu Script", "Roman Urdu", "English"]
lang_exec = [97.4, 94.9, 94.9]
lang_ans = [97.4, 94.9, 94.9]

fig, ax = plt.subplots(figsize=(7.5, 4.8), dpi=300)

x_lang = np.arange(len(lang_labels))
bar_w = 0.32

r_l1 = ax.bar(x_lang - bar_w/2, lang_exec, bar_w, label="Execution Accuracy (%)", color=COLOR_NAVY, zorder=3)
r_l2 = ax.bar(x_lang + bar_w/2, lang_ans, bar_w, label="Answer Accuracy (%)", color=COLOR_TEAL, zorder=3)

ax.set_ylabel("Accuracy (%)", fontweight="bold", labelpad=8)
ax.set_title("Multilingual Performance Distribution\n(Urdu Script, Roman Urdu, and English)", fontweight="bold", pad=12, fontsize=11)
ax.set_xticks(x_lang)
ax.set_xticklabels(lang_labels, fontweight="bold", fontsize=9.5)
ax.set_ylim(75, 105)
ax.set_yticks(np.arange(75, 106, 5))
ax.legend(loc="lower right", framealpha=0.95, edgecolor="#CBD5E1")

# Reference line at 90%
ax.axhline(90, color="#94A3B8", linestyle=":", linewidth=1.2, alpha=0.75, zorder=2)
ax.text(-0.35, 90.6, "90% Benchmark", color="#64748B", fontsize=8, fontstyle="italic")

for r in [r_l1, r_l2]:
    for rect in r:
        h = rect.get_height()
        ax.annotate(f"{h:.1f}%",
                    xy=(rect.get_x() + rect.get_width() / 2, h),
                    xytext=(0, 3.5),
                    textcoords="offset points",
                    ha="center", va="bottom",
                    fontsize=8.0, fontweight="bold",
                    color=COLOR_SLATE)

fig.tight_layout()
fig4_10_path = os.path.join("figures", "figure_4_10_multilingual_distribution.png")
fig.savefig(fig4_10_path, dpi=300, bbox_inches="tight")
plt.close(fig)
print(f"Generated: {fig4_10_path}")


# ==============================================================================
# Figure 4.11: Mean and Median Latency Distribution by Query Classification
# ==============================================================================
latency_groups = [
    ("Direct\nRetrieval", 1.32, 1.29),
    ("Relational\nJOIN", 0.93, 0.90),
    ("Aggregation\n& Grouping", 1.44, 1.33),
    ("Comparison\n& Ranking", 2.64, 1.76),
    ("Date & Multi\nConditions", 1.70, 1.35),
]

lat_labels = [g[0] for g in latency_groups]
lat_mean = [g[1] for g in latency_groups]
lat_median = [g[2] for g in latency_groups]

fig, ax = plt.subplots(figsize=(9.2, 4.8), dpi=300)

x_lat = np.arange(len(lat_labels))
bar_wl = 0.34

r_m1 = ax.bar(x_lat - bar_wl/2, lat_mean, bar_wl, label="Mean Latency (s)", color=COLOR_SLATE, zorder=3)
r_m2 = ax.bar(x_lat + bar_wl/2, lat_median, bar_wl, label="Median Latency (s)", color=COLOR_TEAL, zorder=3)

ax.set_ylabel("Response Latency (Seconds)", fontweight="bold", labelpad=8)
ax.set_title("Mean and Median Latency Distribution by Query Classification", fontweight="bold", pad=12, fontsize=11)
ax.set_xticks(x_lat)
ax.set_xticklabels(lat_labels, fontsize=9.2)
ax.set_ylim(0, 3.5)
ax.set_yticks(np.arange(0, 3.6, 0.5))

# Upper left legend avoids all bars: Group 1 is 1.32s, Group 2 is 0.93s, plenty of headroom up to 3.5s
ax.legend(loc="upper left", framealpha=0.95, edgecolor="#CBD5E1")

for r in [r_m1, r_m2]:
    for rect in r:
        h = rect.get_height()
        ax.annotate(f"{h:.2f}s",
                    xy=(rect.get_x() + rect.get_width() / 2, h),
                    xytext=(0, 3.5),
                    textcoords="offset points",
                    ha="center", va="bottom",
                    fontsize=8.0, fontweight="bold",
                    color=COLOR_SLATE)

fig.tight_layout()
fig4_11_path = os.path.join("figures", "figure_4_11_latency_distribution.png")
fig.savefig(fig4_11_path, dpi=300, bbox_inches="tight")
plt.close(fig)
print(f"Generated: {fig4_11_path}")
print("All 3 figures re-generated cleanly at 300 DPI.")
