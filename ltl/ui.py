"""Wygląd aplikacji: style, nagłówek, karty ze wskaźnikami i pomocnicze elementy nawigacji między etapami."""
from __future__ import annotations

import html

import streamlit as st

# etapy wyboru wariantu = zakładki u góry strony (kolejność = kolejność pracy)
STAGES = ["📥 1 · Dane", "📝 2 · Zlecenia", "🏁 3 · Start i punkty", "🏆 4 · Warianty", "🗺️ 5 · Trasa"]
STAGE_KEY = "stage"

CSS = """
<style>
  :root { --brand:#2563eb; --brand-2:#6366f1; --ink:#0f172a; --muted:#64748b; --line:#dbe2ee; --card:#ffffff;
          --soft:#f1f5fb; --ok:#16a34a; --bad:#dc2626; --warn:#d97706; }
  html, body, [class*="st-"] { font-feature-settings: "cv11", "ss01"; }
  .block-container { padding-top: 1.4rem; padding-bottom: 4rem; max-width: 1280px; }
  h1, h2, h3 { letter-spacing: -.01em; }

  /* nagłówek */
  .hero { background: linear-gradient(120deg, #1e40af 0%, #2563eb 45%, #6366f1 100%); color: #fff;
          border-radius: 20px; padding: 1.1rem 1.5rem; margin: 0 0 .6rem 0; box-shadow: 0 10px 30px rgba(37,99,235,.25); }
  .hero .t { font-size: 1.6rem; font-weight: 750; line-height: 1.15; display: flex; align-items: center; gap: .6rem; }
  .hero .s { opacity: .88; margin-top: .25rem; font-size: 1rem; }
  .hero .chips { display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .8rem; }
  .chip { background: rgba(255,255,255,.18); border: 1px solid rgba(255,255,255,.28); padding: .18rem .7rem;
          border-radius: 999px; font-size: .88rem; backdrop-filter: blur(4px); }

  /* zakładki etapów: pasek zostaje na górze przy przewijaniu */
  .st-key-stage, .st-key-stage > div { overflow: visible !important; }
  .st-key-stage > div > [role="tablist"] { position: sticky; top: 0; z-index: 60; gap: .35rem; padding: .45rem 0 0 0;
          background: #f4f6fb; box-shadow: 0 6px 10px -8px rgba(15,23,42,.25); border-bottom: 1px solid var(--line); }
  [data-testid="stTab"] { border-radius: 12px 12px 0 0; padding: .55rem 1rem; font-weight: 600; cursor: pointer; }
  [data-testid="stTab"] p { font-size: 1rem; font-weight: 600; }
  [data-testid="stTab"]:hover { background: rgba(37,99,235,.05); }
  [data-testid="stTab"][aria-selected="true"] { background: rgba(37,99,235,.09); box-shadow: inset 0 -3px 0 var(--brand); }
  [data-testid="stTab"][aria-selected="true"] p { color: var(--brand); }

  /* nagłówek etapu */
  .anchor { scroll-margin-top: 4rem; }
  .stage { margin: .7rem 0 .9rem 0; }
  .stage .k { color: var(--brand); font-weight: 650; font-size: .82rem; text-transform: uppercase; letter-spacing: .08em; }
  .stage .t { font-size: 1.55rem; font-weight: 720; line-height: 1.2; color: var(--ink); }
  .stage .h { color: var(--muted); margin-top: .2rem; font-size: .98rem; max-width: 70ch; }

  /* karty */
  div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 16px; border-color: var(--line);
          box-shadow: 0 1px 2px rgba(15,23,42,.04), 0 6px 18px rgba(15,23,42,.04); background: var(--card); }
  div[data-testid="stExpander"] { border-radius: 14px; border: 1px solid var(--line); background: var(--card);
          box-shadow: 0 1px 2px rgba(15,23,42,.03); }
  div[data-testid="stExpander"] summary { font-weight: 600; }
  div[data-testid="stAlert"] { border-radius: 14px; }
  div[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; border: 1px solid var(--line); }
  div[data-testid="stForm"] { border: none; padding: 0; }
  textarea, input { border-radius: 10px !important; }
  .stButton > button, .stDownloadButton > button, div[data-testid="stFormSubmitButton"] > button {
          border-radius: 12px; font-weight: 600; padding: .55rem 1.1rem; transition: box-shadow .15s, transform .05s; }
  .stButton > button:hover, div[data-testid="stFormSubmitButton"] > button:hover { box-shadow: 0 4px 14px rgba(37,99,235,.25); }
  .stButton > button:active { transform: translateY(1px); }
  button[kind="primary"], button[kind="primaryFormSubmit"] { background: linear-gradient(120deg, #2563eb, #4f46e5); border: none; }

  /* karty ze wskaźnikami */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); gap: .75rem; margin: .4rem 0 .9rem 0; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: .8rem 1rem;
          box-shadow: 0 1px 2px rgba(15,23,42,.04); }
  .card .l { color: var(--muted); font-size: .82rem; font-weight: 550; }
  .card .v { font-size: 1.4rem; font-weight: 720; line-height: 1.25; color: var(--ink); white-space: nowrap; }
  .card .d { font-size: .82rem; color: var(--muted); margin-top: .1rem; }
  .card.pos .v, .card .d.pos { color: var(--ok); } .card.neg .v, .card .d.neg { color: var(--bad); }
  .card.accent { border-color: rgba(37,99,235,.35); background: linear-gradient(180deg, rgba(37,99,235,.06), #fff); }

  .best { border: 1px solid rgba(22,163,74,.35); background: linear-gradient(180deg, rgba(22,163,74,.08), #fff);
          border-radius: 16px; padding: .85rem 1.1rem; margin-bottom: .9rem; box-shadow: 0 1px 2px rgba(15,23,42,.04); }
  .best .big { font-size: 1.15rem; font-weight: 700; }
  .best .sub { color: var(--muted); margin-top: .15rem; }
  .chain { margin: .2rem 0 .8rem 0; line-height: 2; background: var(--soft); border-radius: 12px; padding: .5rem .9rem; }
  .empty { text-align: center; padding: 2.5rem 1rem; color: var(--muted); border: 2px dashed var(--line);
           border-radius: 18px; background: rgba(255,255,255,.6); }
  .empty .i { font-size: 2.4rem; }
  .empty .m { margin-top: .4rem; font-size: 1.05rem; }

  /* panel boczny */
  section[data-testid="stSidebar"] { border-right: 1px solid var(--line); }
  section[data-testid="stSidebar"] h1 { font-size: 1.25rem; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def hero(title: str, subtitle: str, chips: list[str] | None = None) -> None:
    chips_html = "".join(f"<span class='chip'>{html.escape(c)}</span>" for c in (chips or []))
    st.markdown(f"<div class='hero'><div class='t'>{title}</div><div class='s'>{html.escape(subtitle)}</div>"
                f"{'<div class=chips>' + chips_html + '</div>' if chips_html else ''}</div>", unsafe_allow_html=True)


def step(n, title: str, hint: str = "") -> None:
    """Nagłówek etapu wewnątrz zakładki (kropka numeru + tytuł + krótki opis)."""
    label = f"Etap {n} z {len(STAGES)}" if isinstance(n, int) else str(n)
    st.markdown(f"<div id='krok-{n}' class='anchor'></div><div class='stage'><div class='k'>{label}</div>"
                f"<div class='t'>{title}</div>{f'<div class=h>{hint}</div>' if hint else ''}</div>", unsafe_allow_html=True)


def cards(items: list[tuple]) -> None:
    """Karty ze wskaźnikami. items: (etykieta, wartość, podpis | None, 'pos' | 'neg' | 'accent' | '', podpowiedź)."""
    parts = []
    for label, value, delta, tone, help_ in items:
        delta_cls = tone if tone in ("pos", "neg") else ""
        delta_html = f"<div class='d {delta_cls}'>{delta}</div>" if delta else ""
        parts.append(f"<div class='card {tone}' title='{html.escape(help_ or '')}'><div class='l'>{label}</div>"
                     f"<div class='v'>{value}</div>{delta_html}</div>")
    st.markdown(f"<div class='cards'>{''.join(parts)}</div>", unsafe_allow_html=True)


kpis = cards          # zgodność wsteczna


def empty_state(icon: str, message: str) -> None:
    st.markdown(f"<div class='empty'><div class='i'>{icon}</div><div class='m'>{message}</div></div>", unsafe_allow_html=True)


def go_to(stage_index: int) -> None:
    """Callback przycisków „Dalej”: przełącza zakładkę (musi być ustawione przed utworzeniem zakładek, więc działa jako callback)."""
    st.session_state[STAGE_KEY] = STAGES[stage_index]


def next_button(label: str, stage_index: int, key: str, primary: bool = True) -> None:
    st.button(label, key=key, on_click=go_to, args=(stage_index,), type="primary" if primary else "secondary")
