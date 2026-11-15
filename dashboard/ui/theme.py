"""
dashboard/ui/theme.py — the dashboard's look (04/11, the owner: "ממש ישן", "עיצוב חדשני").

The same language as the study site (scripts/site_assets/app.css): OKLCH tokens, Rubik for headings, IBM Plex Sans
Hebrew for text, JetBrains Mono for numbers, rounded tiles, a glass top bar, pills for tabs. Light and dark follow the
system (.streamlit/config.toml has both). The token names the views use inline (--bg, --raised, --border, --text,
--muted, --amber, --green …) stay; --amber is the one accent, whatever its name says.
FRIEND_CSS / FRIEND_JS are behaviour, not look: they hide alerts and chrome from the friend role.
"""
from __future__ import annotations

import streamlit as st

_FONTS = ("@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Hebrew:wght@400;500;600&"
          "family=Rubik:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');")

_LIGHT = """
  --bg:oklch(98.2% .005 var(--h));--surface:oklch(95.6% .01 var(--h));--raised:oklch(100% 0 0);
  --overlay:oklch(93.5% .012 var(--h));--hover:oklch(96% .012 var(--h));
  --border:oklch(90.5% .012 var(--h));--border2:oklch(83% .016 var(--h));--border-hi:oklch(70% .02 var(--h));
  --text:oklch(23% .022 var(--h));--text-2:oklch(42% .02 var(--h));--muted:oklch(52% .018 var(--h));--dim:oklch(70% .016 var(--h));
  --amber:oklch(50% .15 var(--h));--amber-d:oklch(44% .15 var(--h));--amber-l:oklch(58% .14 var(--h));--amber-dim:oklch(94.5% .03 var(--h));
  --primary-soft:oklch(94.5% .03 var(--h));--primary-soft2:oklch(90% .055 var(--h));--acc-text:oklch(43% .15 var(--h));--on-primary:#fff;
  --green:oklch(50% .13 155);--green-dim:oklch(95% .03 155);--red:oklch(53% .18 25);--red-dim:oklch(95% .025 25);
  --blue:oklch(52% .11 235);--blue-dim:oklch(95% .02 235);--purple:oklch(50% .13 300);--violet-soft:oklch(95% .025 300);
  --warning:oklch(56% .13 70);--warning-soft:oklch(95.5% .045 90);
  --glass:oklch(100% 0 0 / .78);
  --shadow:0 1px 2px oklch(30% .03 var(--h) / .06),0 8px 28px oklch(30% .03 var(--h) / .08);
  --shadow-lg:0 18px 60px oklch(25% .04 var(--h) / .2);color-scheme:light;
"""
_DARK = """
  --bg:oklch(17.5% .014 var(--h));--surface:oklch(21% .018 var(--h));--raised:oklch(21.5% .018 var(--h));
  --overlay:oklch(25% .02 var(--h));--hover:oklch(25% .02 var(--h));
  --border:oklch(29% .02 var(--h));--border2:oklch(37% .024 var(--h));--border-hi:oklch(48% .026 var(--h));
  --text:oklch(93% .008 var(--h));--text-2:oklch(77% .014 var(--h));--muted:oklch(66% .016 var(--h));--dim:oklch(50% .018 var(--h));
  --amber:oklch(74% .13 var(--h));--amber-d:oklch(68% .13 var(--h));--amber-l:oklch(80% .11 var(--h));--amber-dim:oklch(27% .05 var(--h));
  --primary-soft:oklch(27% .05 var(--h));--primary-soft2:oklch(33% .07 var(--h));--acc-text:oklch(82% .1 var(--h));--on-primary:oklch(18% .03 var(--h));
  --green:oklch(76% .14 155);--green-dim:oklch(27% .04 155);--red:oklch(73% .16 25);--red-dim:oklch(28% .05 25);
  --blue:oklch(75% .1 235);--blue-dim:oklch(27% .04 235);--purple:oklch(76% .11 300);--violet-soft:oklch(27% .04 300);
  --warning:oklch(79% .13 80);--warning-soft:oklch(29% .05 85);
  --glass:oklch(23% .02 var(--h) / .78);
  --shadow:0 1px 2px #0005,0 8px 28px #0004;--shadow-lg:0 18px 60px #0009;color-scheme:dark;
"""

_CSS = _FONTS + """
:root{--h:250;""" + _LIGHT + """
  --primary:var(--amber);--primary-strong:var(--amber-d);--primary-ring:var(--primary-soft2);
  --success:var(--green);--success-soft:var(--green-dim);--danger:var(--red);--danger-soft:var(--red-dim);
  --info:var(--blue);--info-soft:var(--blue-dim);--violet:var(--purple);--indigo:var(--amber);--indigo-l:var(--amber-l);
  --surface-2:var(--surface);--surface-3:var(--overlay);--border-strong:var(--border2);
  --shadow-sm:var(--shadow);--shadow-md:var(--shadow);--qc-red:var(--amber);
  --font:"IBM Plex Sans Hebrew","Segoe UI",Arial,sans-serif;--disp:"Rubik","IBM Plex Sans Hebrew",sans-serif;
  --mono:"JetBrains Mono",Consolas,Menlo,monospace;
  --radius-xs:7px;--radius-sm:10px;--radius:12px;--radius-lg:18px;--radius-xl:24px;
  --r-xs:7px;--r-sm:10px;--r-md:12px;--r-lg:18px;--r-xl:24px;
  --text-xs:11.5px;--text-sm:12.5px;--text-base:14px;--text-md:15px;--text-lg:17px;--text-xl:21px;--text-2xl:27px;--text-3xl:35px;
}
@media (prefers-color-scheme:dark){:root{""" + _DARK + """}}

/* ── page + RTL ─────────────────────────────────────────── */
html,body,.stApp,[class*="css"]{direction:rtl;font-family:var(--font);-webkit-font-smoothing:antialiased}
.stApp,[data-testid="stAppViewContainer"],[data-testid="stMain"]{background:var(--bg)!important;color:var(--text)}
[data-testid="stMainBlockContainer"],.main .block-container{padding:0 26px 70px!important;max-width:1280px}
[data-testid="stHeader"]{background:transparent!important;height:0!important;min-height:0}
[data-testid="stDecoration"],[data-testid="stToolbar"],footer{display:none!important}
[data-testid="stSidebar"],[data-testid="stSidebarCollapsedControl"],[data-testid="stExpandSidebarButton"]{display:none!important}
p,li,td,th,caption,figcaption,blockquote,dt,dd,h1,h2,h3,h4,h5,h6{direction:rtl;text-align:right}
[data-testid="stElementContainer"],[data-testid="stMarkdownContainer"]{text-align:start}
[data-testid="stMarkdownContainer"] p{line-height:1.65}
[data-testid="stWidgetLabel"],[data-testid="stWidgetLabel"] p,label{direction:rtl!important;text-align:right!important;color:var(--text-2)}
[data-testid="stHorizontalBlock"]{direction:rtl!important}
h1,h2,h3,h4{font-family:var(--disp)!important;color:var(--text);letter-spacing:-.005em}
h1{font-size:1.9rem!important}h2{font-size:1.45rem!important}h3{font-size:1.18rem!important}
a{color:var(--acc-text)}
code,pre,.mono{font-family:var(--mono)!important}
pre,[data-testid="stCode"] pre{direction:ltr;text-align:left;border-radius:var(--radius)}
code{background:var(--surface);border-radius:6px;padding:1px 5px;color:var(--text);direction:ltr;unicode-bidi:embed}
::-webkit-scrollbar{width:7px;height:7px}::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:7px}
hr{border-color:var(--border)!important}
/* Vega draws on a canvas that inherits `direction`; under rtl Hebrew axis labels run into the bars */
[data-testid="stVegaLiteChart"],[data-testid="stArrowVegaLiteChart"],.vega-embed{direction:ltr}
[data-testid="stDataFrame"] th,[data-testid="stDataFrame"] td{text-align:right!important}
[data-testid="stDataFrame"]{border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
[data-testid="stMetricDelta"] svg{transform:none!important}

/* ── the glass top bar ──────────────────────────────────── */
[data-testid="stLayoutWrapper"]:has(>.st-key-topbar){position:sticky;top:0;z-index:50}
.st-key-topbar{margin:0 -26px 18px;padding:9px 26px;width:calc(100% + 52px)!important;
  background:var(--glass);backdrop-filter:blur(16px) saturate(1.4);-webkit-backdrop-filter:blur(16px) saturate(1.4);border-bottom:1px solid var(--border);
  box-shadow:0 0 0 100vmax var(--glass);clip-path:inset(0 -100vmax)}
.ssa-head{display:flex;align-items:center;gap:12px;min-width:0;direction:rtl}
.ssa-head .mk{width:36px;height:36px;border-radius:12px;background:var(--amber);color:var(--on-primary);display:grid;place-items:center;flex:none}
.ssa-head .mk svg{width:21px;height:21px}
.ssa-head .nm{font:600 1.08rem/1.2 var(--disp);color:var(--text);white-space:nowrap}
.ssa-head .now{font-size:12.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ssa-head .tx{min-width:0;flex:1}
.ssa-head .go{flex:none;display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:12px;background:var(--primary-soft);color:var(--acc-text);
  font-size:13.5px;font-weight:600;text-decoration:none;transition:background .15s}
.ssa-head .go:hover{background:var(--primary-soft2)}
.ssa-head .role{flex:none;font-size:12px;color:var(--text-2);background:var(--surface);padding:3px 9px;border-radius:9px}

/* ── tabs = pills (1.59: react-aria — [role=tablist], stTab, stTabPanel; a tab is a div, not a button) ── */
[data-testid="stTabs"] [role="tablist"]{gap:2px;background:var(--surface);border-radius:15px;padding:4px;border:0;width:fit-content;max-width:100%;
  overflow-x:auto;scrollbar-width:none;margin-bottom:6px;box-shadow:none}
[data-testid="stTabs"] [role="tablist"]::-webkit-scrollbar{display:none}
[data-testid="stTabs"] [role="tablist"]::before,[data-testid="stTabs"] [role="tablist"]::after{display:none!important}
[data-testid="stTab"]{direction:rtl;font-size:13.5px;font-weight:500;height:auto;min-height:0;padding:7px 14px!important;margin:0!important;border-radius:11px;
  color:var(--text-2);background:transparent;border:0!important;box-shadow:none;white-space:nowrap;cursor:pointer;transition:background .15s,color .15s}
[data-testid="stTab"]>div:not([data-testid]){display:none!important}   /* the sliding underline */
[data-testid="stTab"] p{font-size:13.5px;margin:0;color:inherit;font-weight:inherit}
[data-testid="stTab"]:hover{color:var(--text)}
[data-testid="stTab"][aria-selected="true"]{color:var(--acc-text)!important;background:var(--raised)!important;font-weight:600;
  box-shadow:0 1px 3px oklch(30% .03 var(--h) / .14)}
[data-testid="stTabPanel"]{border:0;padding:16px 0 0!important;background:transparent}

/* ── widgets ────────────────────────────────────────────── */
.stButton>button,.stDownloadButton>button,.stFormSubmitButton>button,.stLinkButton>a,
[data-testid^="stBaseButton"]{font-family:var(--font);font-weight:600;border-radius:12px!important;min-height:38px;transition:transform .12s,filter .15s,background .15s}
[data-testid="stBaseButton-secondary"],[data-testid="stBaseButton-secondaryFormSubmit"]{background:var(--surface)!important;color:var(--text)!important;border:1px solid transparent!important}
[data-testid="stBaseButton-secondary"]:hover,[data-testid="stBaseButton-secondaryFormSubmit"]:hover{background:var(--primary-soft)!important;color:var(--acc-text)!important}
[data-testid="stBaseButton-primary"],[data-testid="stBaseButton-primaryFormSubmit"]{background:var(--amber)!important;color:var(--on-primary)!important;border:0!important}
[data-testid="stBaseButton-primary"] p,[data-testid="stBaseButton-primaryFormSubmit"] p{color:var(--on-primary)!important}
[data-testid="stBaseButton-primary"]:hover,[data-testid="stBaseButton-primaryFormSubmit"]:hover{filter:brightness(1.08)}
[data-testid^="stBaseButton"]:active{transform:scale(.97)}
[data-baseweb="input"],[data-baseweb="base-input"],[data-baseweb="textarea"],[data-baseweb="select"]>div{background:var(--raised)!important;border-radius:12px!important;
  border-color:var(--border2)!important;color:var(--text)!important}
[data-baseweb="input"]:focus-within,[data-baseweb="textarea"]:focus-within,[data-baseweb="select"]>div:focus-within{border-color:var(--amber)!important;box-shadow:0 0 0 3px var(--primary-ring)!important}
input,textarea{font-family:var(--font)!important;color:var(--text)!important;direction:rtl;text-align:right;caret-color:var(--amber)!important}
input::placeholder,textarea::placeholder{color:var(--muted)!important}
input[type="number"],input[type="email"],input[type="password"]{unicode-bidi:plaintext}
[data-baseweb="select"],[data-baseweb="select"] *{direction:rtl!important}
[data-baseweb="popover"]>div,[data-baseweb="menu"]{background:var(--raised)!important;border-radius:14px!important;box-shadow:var(--shadow-lg)!important}
[data-baseweb="popover"] [role="option"]{text-align:right!important;direction:rtl!important;color:var(--text);border-radius:9px}
[data-baseweb="popover"] [role="option"]:hover,[data-baseweb="popover"] [aria-selected="true"]{background:var(--primary-soft)!important}
[data-baseweb="tag"]{background:var(--primary-soft)!important;color:var(--acc-text)!important;border-radius:8px!important}
[data-baseweb="tag"] span{color:var(--acc-text)!important}
[data-baseweb="checkbox"]>div:first-child{border-radius:6px}
[data-testid="stFileUploaderDropzone"]{background:var(--raised);border:1.5px dashed var(--border2);border-radius:var(--radius-lg)}
[data-testid="stExpander"]{background:transparent}
[data-testid="stExpander"] details{background:var(--raised);border:1px solid var(--border)!important;border-radius:var(--radius)!important;overflow:hidden}
[data-testid="stExpander"] summary{font-weight:600;color:var(--text)}
[data-testid="stExpander"] summary:hover{background:var(--hover)}
[data-testid="stAlert"]>div,.stAlert>div{border-radius:var(--radius)!important;border:0}
[data-testid="stAlertContainer"]{border-radius:var(--radius)!important;direction:rtl;text-align:right}
[data-testid="stMetric"]{background:var(--raised);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 18px}
[data-testid="stMetricLabel"] p{color:var(--muted);font-size:13px}
[data-testid="stMetricValue"]{font-family:var(--disp);font-weight:700;color:var(--text);font-size:1.9rem}
[data-testid="stProgress"] [role="progressbar"]>div>div>div{background:var(--amber)!important}
[data-testid="stChatMessage"]{background:var(--raised);border:1px solid var(--border);border-radius:var(--radius-lg);direction:rtl}
[data-testid="stChatInput"]{border-radius:var(--radius-lg)}
[data-testid="stVerticalBlockBorderWrapper"]{border-radius:var(--radius-lg)!important;border-color:var(--border)!important}
[class*="st-key-card_"]{background:var(--raised)!important;border:1px solid var(--border)!important;border-radius:var(--radius-lg)!important;padding:16px 18px;box-shadow:none!important}
.st-key-card_login{box-shadow:var(--shadow)!important}
table{border-collapse:collapse}
[data-testid="stMarkdownContainer"] table,[data-testid="stTable"] table{width:100%;font-size:13.5px}
[data-testid="stMarkdownContainer"] th,[data-testid="stTable"] th{background:var(--surface);color:var(--text-2);font-weight:600}
[data-testid="stMarkdownContainer"] td,[data-testid="stMarkdownContainer"] th,[data-testid="stTable"] td,[data-testid="stTable"] th{border:0;border-bottom:1px solid var(--border);padding:8px 11px}

/* ── ssa-* components ───────────────────────────────────── */
.ssa-brand-mark{width:48px;height:48px;display:grid;place-items:center;background:var(--amber);color:var(--on-primary);border-radius:16px}
.ssa-brand-mark svg{width:26px;height:26px}
.ssa-ph{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:6px 0 18px;direction:rtl}
.ssa-ph-main{display:flex;gap:12px;align-items:center;min-width:0}
.ssa-ph-icon{font-size:24px;line-height:1}
.ssa-eyebrow{font-size:12.5px;color:var(--muted);margin-bottom:2px}
.ssa-ph-title{font:700 1.8rem/1.2 var(--disp);color:var(--text);letter-spacing:-.01em}
.ssa-ph-sub{font-size:13.5px;color:var(--text-2);margin-top:4px}
.ssa-sec,.section-title{margin:26px 0 12px;direction:rtl}
.ssa-sec{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
.ssa-sec-title,.section-title{display:flex;align-items:center;gap:9px;font:600 1.12rem/1.3 var(--disp);color:var(--text)}
.ssa-sec-sub{font-size:12.5px;color:var(--muted)}
.ssa-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,var(--min,220px)),1fr));gap:13px;margin-bottom:18px}
.ssa-card{background:var(--raised);border:1px solid var(--border);border-radius:var(--radius-lg);padding:17px 19px;min-width:0}
.ssa-stat{display:flex;flex-direction:column;gap:5px}
.ssa-stat-top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.ssa-stat-label{font-size:13px;color:var(--muted)}
.ssa-stat-icon{font-size:16px}
.ssa-stat-value{font:700 2.1rem/1.08 var(--disp);color:var(--text);font-variant-numeric:tabular-nums;letter-spacing:-.015em}
.ssa-stat-value small{font-size:.9rem;font-weight:500;color:var(--muted)}
.ssa-stat-sub{font-size:12.5px;color:var(--muted);line-height:1.5}
.ssa-tone-success .ssa-stat-value{color:var(--green)}.ssa-tone-warning .ssa-stat-value{color:var(--warning)}
.ssa-tone-danger .ssa-stat-value{color:var(--red)}.ssa-tone-primary .ssa-stat-value{color:var(--acc-text)}
.ssa-meter{height:7px;background:var(--surface);border-radius:7px;overflow:hidden;margin:8px 0 4px;position:relative}
.ssa-meter>span{display:block;height:100%;background:var(--amber);border-radius:7px}
.ssa-meter.success>span{background:var(--green)}.ssa-meter.warning>span{background:var(--warning)}.ssa-meter.danger>span{background:var(--red)}
.ssa-meter .ssa-tick{position:absolute;top:0;bottom:0;width:2px;background:var(--text)}
.ssa-pill,.badge{display:inline-flex;align-items:center;gap:4px;white-space:nowrap;padding:2px 9px;margin:1px 2px;border-radius:8px;font-size:12px;font-weight:500;line-height:1.6;
  background:var(--surface);color:var(--text-2);border:0;unicode-bidi:isolate}
.ssa-pill.success,.badge-moodle,.badge-sent,.badge-up{background:var(--green-dim);color:var(--green)}
.ssa-pill.warning{background:var(--warning-soft);color:var(--warning)}
.ssa-pill.danger,.badge-unsent,.badge-down{background:var(--red-dim);color:var(--red)}
.ssa-pill.primary,.badge-book{background:var(--primary-soft);color:var(--acc-text)}
.ssa-pill.info,.badge-wikidot,.badge-board{background:var(--blue-dim);color:var(--blue)}
.ssa-pill.violet,.badge-whisper{background:var(--violet-soft);color:var(--purple)}
.ssa-dot{width:7px;height:7px;border-radius:50%;display:inline-block;background:var(--muted);flex-shrink:0}
.ssa-dot.success{background:var(--green)}.ssa-dot.warning{background:var(--warning)}.ssa-dot.danger{background:var(--red)}.ssa-dot.primary{background:var(--amber)}
.ssa-list{background:var(--raised);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden}
.ssa-list-head{display:flex;justify-content:space-between;gap:10px;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--surface);font-size:12.5px;color:var(--muted);flex-wrap:wrap}
.ssa-row{display:flex;align-items:center;gap:12px;padding:11px 16px;border-bottom:1px solid var(--border);min-width:0}
.ssa-row:last-child{border-bottom:0}.ssa-row:hover,.feed-row:hover{background:var(--hover)!important}
.ssa-row-main{flex:1;min-width:0}
.ssa-row-title{font-size:14px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ssa-row-sub{font-size:12.5px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ssa-row-date{font:500 12px var(--mono);color:var(--muted);flex-shrink:0;min-width:38px;direction:ltr;text-align:center}
.ssa-row-end{flex-shrink:0;display:flex;gap:6px;align-items:center}
.ssa-chip-course{font-size:12px;font-weight:600;color:var(--acc-text);flex-shrink:0}
.ssa-empty{text-align:center;padding:40px 18px;color:var(--muted);background:var(--raised);border:1.5px dashed var(--border2);border-radius:var(--radius-lg)}
.ssa-empty-icon{font-size:32px;margin-bottom:10px}.ssa-empty-title{font-size:15.5px;font-weight:600;color:var(--text-2)}.ssa-empty-text{font-size:13px;margin-top:6px}
.ssa-callout{display:flex;gap:10px;align-items:flex-start;padding:12px 16px;margin-bottom:10px;font-size:14px;color:var(--text);background:var(--surface);border-radius:var(--radius)}
.ssa-callout-icon{font-size:15px;line-height:1.5}
.ssa-callout.warning{background:var(--warning-soft)}.ssa-callout.danger{background:var(--red-dim)}
.ssa-callout.success{background:var(--green-dim)}.ssa-callout.primary,.ssa-callout.info{background:var(--primary-soft)}
.ssa-course{display:flex;flex-direction:column;gap:8px}.ssa-course-head{display:flex;gap:10px;align-items:center}
.ssa-course-emoji{font-size:1.3rem;line-height:1;flex-shrink:0}.ssa-course-name{font:600 1.05rem var(--disp);color:var(--text)}.ssa-course-sub{font-size:12.5px;color:var(--muted)}
.ssa-sum{display:flex;flex-direction:column;gap:6px;flex:1}.ssa-sum-title{font-size:14.5px;font-weight:500;color:var(--text);line-height:1.45}
.ssa-sum-meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:12.5px;color:var(--muted)}.ssa-sum-meta .sep{color:var(--border2)}
.ssa-type-bar{width:4px;align-self:stretch;background:var(--amber);border-radius:4px;flex-shrink:0}.ssa-type-bar.tutorial{background:var(--green)}
.ssa-score{text-align:center;padding:24px;background:var(--raised);border:1px solid var(--border);border-radius:var(--radius-lg);margin:10px 0}
.ssa-score-value{font:700 3rem var(--disp)}.ssa-score-sub{color:var(--text-2);margin-top:6px}

/* the views' own classes */
.phase-bar{display:flex;gap:6px;margin:12px 0}
.phase-item{flex:1;text-align:center;padding:9px 6px;border-radius:var(--radius);font-size:12.5px;font-weight:500;transition:all .25s}
.phase-idle{background:var(--surface);color:var(--muted)}
.phase-active{background:var(--primary-soft);color:var(--acc-text);animation:ssa-pulse 2s ease-in-out infinite}
.phase-done{background:var(--green-dim);color:var(--green)}.phase-error{background:var(--red-dim);color:var(--red)}
@keyframes ssa-pulse{50%{opacity:.65}}
.qc-section-title{font:600 1.35rem var(--disp);color:var(--text)!important;margin:.6rem 0 .4rem}
.qc-rule{border:0;border-top:1px solid var(--border);margin:.9rem 0}
.qc-h3{font:600 1.02rem var(--disp);color:var(--text);margin:.4rem 0 .5rem}
.qc-meta{color:var(--muted);font-size:.84rem}
.qc-link{color:var(--acc-text)!important;font-size:.85rem;text-underline-offset:3px}
.qc-brain-card{background:var(--raised);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 15px 10px;margin-bottom:.4rem;min-height:200px}
.qc-brain-head{display:flex;align-items:baseline;gap:.45rem;margin-bottom:.5rem}
.qc-brain-emoji{font-size:1.4rem;line-height:1}.qc-brain-title{font:600 1.05rem var(--disp);color:var(--text)}
.qc-brain-stats{width:100%;border-collapse:collapse;font-size:.88rem}
.qc-brain-stats td{padding:.18rem 0;color:var(--text-2);border:0!important;background:transparent!important}
.qc-brain-stats td:last-child{text-align:left;color:var(--text)!important;font-weight:600;font-variant-numeric:tabular-nums}
.qc-meta-card{background:var(--primary-soft);border-radius:var(--radius-lg);padding:14px 18px}
.qc-meta-head{display:flex;align-items:baseline;gap:.6rem;margin-bottom:.5rem}
.qc-meta-sub{color:var(--text-2);font-size:.87rem}
.qc-meta-row{display:flex;flex-wrap:wrap;gap:1rem 1.6rem;color:var(--text);font-size:.93rem}
.qc-meta-row b{color:var(--acc-text);font-variant-numeric:tabular-nums}
.qc-diff-bar{background:var(--surface);border-radius:var(--radius);padding:.55rem .9rem;color:var(--text);font-size:.9rem}
.qc-diff-bar b{color:var(--acc-text);font-weight:600}

@media (max-width:760px){
  [data-testid="stMainBlockContainer"],.main .block-container{padding:0 14px 60px!important}
  .st-key-topbar{margin:0 -14px 14px;padding:8px 14px;width:calc(100% + 28px)!important}
  .st-key-topbar [data-testid="stHorizontalBlock"]{flex-wrap:nowrap;gap:8px}
  .st-key-topbar [data-testid="stColumn"]{min-width:0!important;width:auto!important;flex:1 1 0!important}
  .st-key-topbar [data-testid="stColumn"]:last-child{flex:0 0 66px!important}
  .ssa-head{gap:8px}.ssa-head .nm{font-size:1rem}
  .ssa-head .go span{display:none}
  .ssa-ph-title{font-size:1.5rem}.ssa-stat-value{font-size:1.7rem}
  .phase-bar{flex-direction:column}
  [data-testid="stTabs"] [role="tablist"]{width:100%;flex-wrap:nowrap}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

# Friend role: hide notifications / toasts / chrome (unchanged behaviour).
_FRIEND_CSS = """
[data-testid="stToast"], [data-testid="stToastContainer"] { display: none !important; }
[data-testid="stStatusWidget"] { display: none !important; }
[data-testid="stNotification"], [data-testid="stNotificationContainer"], .stNotificationContainer { display: none !important; }
[data-testid="stAlert"], [data-testid="stAlertContainer"], [data-testid="stAlertContentWarning"],
[data-testid="stAlertContentInfo"], [data-testid="stAlertContentError"], .stAlert, div[role="alert"] { display: none !important; }
[data-testid="stExpander"]:has([data-testid*="Alert"]) { display: none !important; }
[data-testid="stExpander"]:has(svg[data-testid*="Warning"]) { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
[data-testid="stHeader"], header[data-testid="stHeader"] { display: none !important; }
[data-testid="stConnectionStatus"] { display: none !important; }
.stException { display: none !important; }
footer { display: none !important; }
"""

_FRIEND_JS = """
<script>
  (function hideAlertsExpander() {
    const tryHide = () => {
      document.querySelectorAll('[data-testid="stExpander"], details, .streamlit-expanderHeader').forEach(el => {
        const txt = (el.innerText || '').trim();
        if (txt.includes('התראות') || /\\d+\\s*alerts?/i.test(txt) || txt.includes('Warning')) {
          el.style.display = 'none';
        }
      });
    };
    tryHide();
    setInterval(tryHide, 1000);
  })();
</script>
"""


def inject(role: str = "admin") -> None:
    """Inject the stylesheet. Call once per script run, early."""
    css = _CSS
    if role == "friend":
        css += _FRIEND_CSS
    st.html(f"<style>{css}</style>")
    if role == "friend":
        # the text-match fallback needs JS; st.markdown keeps the old behaviour
        st.markdown(_FRIEND_JS, unsafe_allow_html=True)


def inject_login() -> None:
    """Styling for the password screen (before the role is known)."""
    st.html(f"<style>{_CSS}</style>")
