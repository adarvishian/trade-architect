# Phase 1 — Risk-Adjusted Position Sizing: Research & Synthesis

**Project:** Trading Architect — Position Sizing Optimization & Trade Evaluation System
**Phase:** 1 of 3 (Deep Research)
**Date:** 2026-05-26
**Scope:** How professional and academic practitioners size positions, place stops, and scale exposure to optimize *risk-adjusted* returns — stress-tested into a single coherent system for a directional trend/momentum trader. Stocks+options lead; futures handled as a separate silo.

---

## 0. How to read this document

This is the evidence base for the system we will design. It is deliberately opinionated where the evidence supports an opinion, and deliberately cautious where the academic record is genuinely contested. Section 1 frames *your* specific problem. Sections 2–5 build up the toolkit (deepest on fractional-risk, Kelly/optimal-f, and account-scaling, per your direction). Section 6 applies it to your two books. Section 7 is the stress test — where each idea breaks. Section 8 is the synthesized system that survives the stress test. Section 9 lists the open decisions I need from you before Phase 2.

A note on intellectual honesty up front: position sizing is one of the few areas of trading where the math is genuinely well-understood, but it is *also* an area thick with folklore presented as fact. I flag the folklore explicitly (Section 7.1) rather than repeating it.

---

## 1. Framing your actual problem

Your stated pain is precise and, importantly, it is the *opposite* of the problem most position-sizing literature is written to solve. The vast majority of risk-management writing exists to stop people from **over**-betting and blowing up. Your problem is chronic **under**-betting: sizing that was calibrated to a smaller account and never scaled up, so each year of equity growth quietly widens the gap between the returns your edge produces and the returns your sizing captures.

This reframing matters because it changes which tools are load-bearing. For an over-bettor, the entire value is in the *ceiling* (Kelly, risk-of-ruin, drawdown caps). For an under-bettor, the value is in (a) a principled, mechanical way to set the *right* size so discretion stops drifting conservative, and (b) a measurement layer that quantifies the cost of past under-sizing — your "alpha left on the table." The system therefore needs two things the typical risk tool does not provide: an explicit **scaling rule tied to equity**, and a **counterfactual evaluation engine** that re-runs history under better sizing.

The second non-obvious consequence: because you are under-betting, the single most dangerous failure mode of the aggressive frameworks (Kelly/optimal-f and their tendency to *over*-bet under estimation error — Section 7.2) is less threatening to you than to most traders, *but* it becomes threatening precisely at the moment the system convinces you to size up. So the system must let you move toward optimal size **on a dimmer switch, not a light switch** — fractional, throttled, and reversible.

---

## 2. Fixed-fractional & R-multiple sizing (the foundation)

### 2.1 The mechanism

Fixed-fractional sizing risks a constant fraction of current equity on every trade. The canonical form: decide a risk budget *f* (commonly 0.5%–2% of equity), define the dollar risk per unit as the distance from entry to stop, and solve for size:

```
Position size (units) = (Equity × f) / (Entry − Stop)      [per-share/per-unit risk]
```

For futures and options the denominator is scaled by point value / multiplier / contract delta (Section 6). The defining property is that **size adapts inversely to volatility and stop width**: a wider stop (more volatile setup) automatically yields a smaller position so that *dollars at risk stay constant*. That single property is why this method dominates practitioner usage — it converts heterogeneous setups into a common risk currency.

### 2.2 R-multiples (Van Tharp) — the measurement layer you actually want

Van Tharp's contribution is less a sizing rule than a **bookkeeping language** that makes sizing analyzable. "R" is the initial dollar risk on a trade (entry-to-stop). Every outcome is then expressed as a multiple of its own initial risk: stopped out = −1R; a winner that made 2.5× the risked amount = +2.5R. Two quantities fall out of this naturally:

- **Expectancy** = mean R per trade — the average return *per unit of risk taken*. This is the cleanest single number for "is the edge real."
- **Distribution of R** — the shape (skew, tail) of outcomes, which is what actually drives how aggressively you can size.

Why this matters for *your* system specifically: R-multiples are the native unit for the evaluation engine. If we log every historical trade as an R-multiple, we can ask the counterfactual question — "what was my realized R-distribution, and what would total P&L have been at 1.5× or 2× the size I actually used?" — without needing live data or re-simulating the market. The edge (R-distribution) is held fixed; only the sizing multiplier changes. That is the mathematical heart of "alpha left on the table."

### 2.3 What's solid vs. what's folklore

Solid: fixed-fractional sizing is the right *default chassis* because it is transparent, auditable, equity-adaptive, and asset-class agnostic. It also degrades gracefully — a slightly-wrong *f* is merely suboptimal, not ruinous (unlike Kelly, Section 7.2).

Folklore (flagged here, dismantled in 7.1): the widely repeated claim — often attributed to Van Tharp — that "position sizing accounts for ~91% of the variability in portfolio performance." This is a garbled echo of the Brinson–Hood–Beebower asset-allocation studies and does not say what it is quoted as saying. We will not build the system's credibility on it.

---

## 3. Kelly criterion & optimal-f (the growth-optimal ceiling)

### 3.1 Kelly

The Kelly criterion (Kelly 1956, popularized for markets by Ed Thorp) gives the bet fraction that maximizes the **long-run geometric growth rate** of capital. For a simple bet with win probability *p*, loss probability *q = 1−p*, and win/loss payoff ratio *b*:

```
f* = (bp − q) / b
```

Maximizing geometric (not arithmetic) growth is the correct objective for a compounding account, because terminal wealth over many trades is a *product* of returns, and the product is maximized by the log-growth-optimal fraction. Thorp's track record (Princeton/Newport and beyond) is the most cited real-world validation that Kelly-style sizing works when the edge is real and the inputs are honest.

### 3.2 Optimal-f and the Leverage Space Model (Ralph Vince)

Ralph Vince generalized Kelly to empirical, non-binary outcomes. **Optimal-f** finds the fixed fraction that maximizes the geometric mean of *your actual realized trade distribution* (your R-multiples), rather than assuming a clean two-outcome bet. The later **Leverage Space Trading Model** extends this to multiple simultaneous instruments and — critically — lets you maximize growth *subject to a drawdown-probability constraint* you choose, rather than accepting the brutal drawdowns of unconstrained optimization.

Optimal-f is conceptually attractive for us because it consumes exactly the data the evaluation engine already produces (the historical R-distribution). But it inherits Kelly's fragility and adds its own (Section 7.2): it is fitted to *past* trades and can badly over-bet if the future tail is worse than the sample, or if the realized worst loss in-sample understates the true worst case.

### 3.3 Why nobody competent bets full Kelly — and why this is the key design constraint

The decisive academic result is MacLean, Ziemba & Blazenko (1992, *Management Science*) and the broader MacLean–Thorp–Ziemba program: **half-Kelly captures ~75% of the growth rate with ~50% of the volatility**, and the penalty structure is wildly asymmetric — **overbetting is far more destructive than underbetting.** A modest estimation error (say, overestimating your edge by 10%) can push full-Kelly sizing ~50% above optimal, and from there a string of losses compounds viciously. Thorp's own framing is that the justification for fractional Kelly is less about formal uncertainty and more about a *behavioral* tendency to overestimate one's edge.

This is the most important single takeaway in the document for your situation. It means the correct destination for an under-bettor is **not** "full Kelly / optimal-f." It is "a *fraction* of Kelly/optimal-f, approached gradually." Fractional Kelly is the natural bridge between your conservative status quo and growth-optimal sizing: it lets the system point at the optimum while keeping you a safe, deliberate distance from it.

---

## 4. Volatility targeting (the modern overlay)

### 4.1 The idea and its strongest evidence

Volatility targeting sizes positions so that each carries a *constant expected risk contribution*: size ∝ target_vol / recent_volatility. When an instrument's volatility rises, exposure is cut; when it falls, exposure grows. For a trend/momentum trader this is especially relevant because of two well-identified academic results:

- **Moskowitz, Ooi & Pedersen (2012), "Time Series Momentum"** (*JFE*): documented persistent trend effects across 58 liquid futures and sized each position at roughly `40% / σ` (annualized-vol scaling). Follow-up work found that **volatility scaling is responsible for a large share of trend-following's risk-adjusted performance** — stripping it out collapsed monthly alpha from ~1.27% to ~0.41% in one analysis, and made the strategy roughly indistinguishable from buy-and-hold. For a momentum trader, this is close to a free lunch in risk-adjusted terms.
- **Moreira & Muir (2017), "Volatility-Managed Portfolios"** (*Journal of Finance*): taking *less* risk when volatility is high raised Sharpe ratios by 50–100% across the market, value, momentum, profitability, and carry factors.

### 4.2 The honest counter-evidence

This is contested territory and the system should not oversell it. Subsequent work (e.g., Cederburg et al., and the *Financial Analysts Journal* "Conditional Volatility Targeting" literature) found that volatility-managed portfolios **do not reliably beat their unmanaged versions out-of-sample**, that the in-sample alphas often come from regressions that aren't tradable in real time, and that naive vol targeting can badly *overshoot* the volatility target. Translation: vol targeting is a strong *risk-normalization* tool (it makes positions comparable and tames tail/drawdown behavior — the part that replicates robustly) but a weaker *return-enhancement* tool (the alpha claims are fragile out-of-sample).

Design consequence: we use volatility as a **sizing normalizer and a comparability layer**, not as a market-timing alpha engine. That is the part of the evidence that survives replication.

---

## 5. Account-scaling rules (your core pain point)

This section directly targets "I leave money on the table as my account grows." Three mechanisms, in increasing sophistication:

**Equity-linked fractional sizing (the baseline fix).** Because fixed-fractional risk is computed off *current* equity, it scales automatically: a 1% risk on a $250k account is 2.5× the dollars of a 1% risk on $100k. The reason this hasn't solved your problem is almost certainly that your *discretionary* sizing never tracked the formula — intuition anchored to dollar amounts that felt comfortable years ago. The first, highest-leverage fix is therefore not a new algorithm; it is **mechanizing the fraction so size grows with equity without requiring a fresh gut-check each time.**

**Anti-martingale / pyramiding (scaling within a trend).** The literature is consistent that the correct progression for trend traders is *anti*-martingale: increase exposure after gains, decrease after losses — "let winners run, cut losers short." Pyramiding adds to *winning* positions (e.g., after the market moves 1R or 1 ATR in favor), with two safety rules that recur everywhere: each add is *smaller* than the last (never an inverted pyramid), and the stop is advanced (often to breakeven) as you add so aggregate open risk stays bounded. This is the mechanism that lets a momentum trader capture the *full* length of a sustained move — which is exactly the "alpha left on the table" you describe.

**Equity tiers / banding (reducing churn).** Recomputing size off equity after every tick creates noisy, fiddly sizing. Practitioners band it: step size up only when equity crosses defined thresholds (tiers), or use a smoothed equity figure. This trades a tiny amount of theoretical optimality for a large amount of usability — directly in line with your non-negotiable usability mandate.

**Portfolio heat (the ceiling that makes scaling safe).** "Portfolio heat" is the sum of open risk across all positions if every stop is hit simultaneously. Five positions at 2% each = 10% heat. The crucial refinement is **correlation**: three tech longs are not three independent 2% bets — in a sector drawdown they behave like one ~6% bet. Common practitioner guardrails: total heat capped around 5–10%, and per-cluster (sector/theme) heat capped so a single correlated shock can't exceed a tolerable loss. For your system this is the governor that allows aggressive *per-trade* scaling without letting aggregate risk run away — you scale individual positions up *toward* the optimum while a heat cap holds total portfolio risk inside a hard limit.

---

## 6. Applying it to your two books

### 6.1 Stocks + options (lead) — the unification problem

**How Alex actually trades options (confirmed).** Purely directional: only *long, naked, out-of-the-money* calls or puts, typically 90–365 DTE. Calls add leverage to a bullish stock view; puts express a bearish/downside view. **Never used to hedge an existing position.** This is the single most important fact for the stocks+options silo, because it removes the entire delta-neutral/hedging framing and replaces it with a **leverage-management** problem: the options are a *capital-efficient substitute for, or amplifier of, directional stock exposure.* Everything below is rewritten around that reality.

Your source-of-truth definition treats one ticker's stock shares + directional options as a *single logical position* sized at the blended level. The clean way to make stock and options commensurable is **delta-adjusted (and optionally beta-weighted) notional exposure**:

- **Delta is additive.** A position's total directional exposure ≈ shares + Σ(option contracts × delta × 100). This collapses "100 shares + 3 long OTM calls at 0.35 delta" into a single *effective-share* / *effective-notional* number — the unified exposure context.md asks for. (Note OTM long options carry *lower* delta, ~0.2–0.4, and high gamma, so effective exposure drifts as the underlying moves — the system should mark delta dynamically, not freeze it at entry.)
- **Beta-weighting** further normalizes across tickers by expressing each position's delta in benchmark-equivalent (e.g., SPY) terms, so a 2.0-beta name with the same dollar delta as a 1.0-beta name is correctly counted as twice the market exposure — what makes *portfolio* heat meaningful across a mixed book.

**The central design tension for a long-OTM-premium book: premium-at-risk ≠ exposure, and they diverge enormously.** There are two distinct denominators and the system must track both, but for *this* book they pull in opposite directions:

1. **Premium-at-risk (bounded max loss).** The most a long option can lose is the premium paid. That is a clean, naturally-defined risk number — and it is the right input for *fractional-risk sizing* and for *portfolio heat* (worst-case loss if it expires worthless). Attractive, but treacherous as the *sole* sizing basis, because OTM premium is cheap.
2. **Delta-adjusted notional (true directional exposure).** A deep-OTM call can control ~100 shares of notional for a tiny fraction of the share cost (the research example: ~$80 of premium replicating ~$6,446 of stock exposure). So sizing purely off premium-at-risk lets you accumulate *massive hidden notional/delta exposure* for a trivial "risk" number — you are not hedging, you are stacking leverage. This is the classic trap of cheap OTM options.

**Design consequence (important):** for Alex's silo, the **binding sizing constraint should be notional/delta exposure (a leverage cap), with premium-at-risk used as the max-loss and heat input — not the other way around.** Practitioner guidance for leverage-via-options frames the limit as a notional-exposure multiple of capital (e.g., conservative ≈1.5–2× notional, moderate ≈2–3×), and active directional options books often keep aggregate beta-weighted exposure under ~40% of net liquidation value. A blended stock+long-call position is *stacked* same-direction leverage on one name, so the leverage cap must apply to the **combined** delta-notional, not to each leg separately.

**The return distribution this book produces — and why it matters for the math.** Long OTM options are *low-win-rate, positively-skewed* instruments: a large share expire worthless (a near-total premium loss, i.e. ≈ −1R on the option leg), offset by occasional large multi-R winners. Long-dated (90–365 DTE) softens but does not remove **theta decay**, which is a persistent drag and means an option can lose most of its value even if the underlying merely goes sideways. Three implications for the system: (a) the option leg's R-distribution is fundamentally different from the stock leg's and must be evaluated separately before blending; (b) extreme payoff skew is exactly the regime where Kelly/optimal-f are most fragile, so **fractional Kelly (½ or less) is not optional here — it's required** (Section 7.2); and (c) "alpha left on the table" for the options book is dominated by *being too small on the rare big winners*, which is a different optimization than the stock book and the evaluation engine should report it that way.

### 6.2 Futures (separate silo)

Futures sizing has its own denominators and must never blend into the equity book:

- **Notional ≠ margin.** The recurring, expensive mistake is sizing off *margin* (the deposit) rather than *notional* (true exposure = price × point value × contracts). Margin-based sizing silently invites over-leverage. The system should display notional and treat margin only as a capital-availability constraint.
- **Volatility/ATR-based sizing is the standard.** Risk per contract = ATR (or stop distance in points) × point value. Size = (silo equity × f) / risk-per-contract. Higher-ATR contracts → fewer contracts to hold dollar risk constant — the same fixed-fractional logic as equities, just with a contract multiplier.
- **Micro contracts** (MES, MNQ, MCL, etc.) materially improve sizing *granularity* for a growing-but-not-institutional account — they let the fractional formula resolve to a tradable integer instead of forcing a too-big/too-small rounding. Worth noting for the design because granularity is a real usability constraint in futures.

The two silos share the *same sizing philosophy* (fractional risk, vol-normalized, equity-scaled, fractional-Kelly-capped) but maintain **separate equity bases, separate heat budgets, and separate evaluation**, exactly as context.md mandates.

---

## 7. Stress test — where each idea breaks

A system is only as good as its behavior in the cases that embarrass its assumptions. Here is where each tool fails and how the synthesized design (Section 8) absorbs the failure.

### 7.1 The "91% of returns is position sizing" claim is false-as-stated
The figure traces to Brinson, Hood & Beebower (1986) and the 1991 update (Brinson, Hood & Singer, ~91.5%). Those studies found that asset allocation explained ~90%+ of the **variance of returns over time** for a set of pension funds — *not* that it determined 90% of return *level*, and *not* a statement about position sizing at all. Jahnke's 1997 critique ("The Asset Allocation Hoax") showed the variance-vs-level conflation directly. **Implication:** position sizing is demonstrably important (it is the lever that turns an edge into compounded dollars), but we will justify the system on the *mechanics of geometric compounding and R-multiple math*, which are sound, rather than on a misquoted statistic. Credibility with a quant-literate user depends on not repeating folklore.

### 7.2 Kelly/optimal-f over-bet under estimation error and fat tails
Both methods assume your estimated edge (and, for optimal-f, your in-sample worst loss) is the truth. Markets deliver fatter tails and regime shifts than any backtest sample. MacLean–Thorp–Ziemba quantify the asymmetry: overbetting can lose many multiples of capital where underbetting merely slows growth. Optimal-f is additionally fragile because it is *fitted* to the historical trade set — a future loss worse than the in-sample max can be catastrophic at the fitted fraction. **Absorbed by:** never sizing at full Kelly/optimal-f; treating it as a *ceiling estimate*; applying a fraction (¼–½); and capping with portfolio heat so a tail event is survivable regardless of the per-trade math.

### 7.3 Volatility targeting's alpha doesn't replicate out-of-sample
As Section 4.2 documents, the return-enhancement claims are contested and often non-tradable in real time; naive implementations overshoot the vol target. **Absorbed by:** using volatility only for *normalization and comparability* (robust) and not as a timing/alpha signal (fragile).

### 7.4 Correlation makes "diversified" heat a lie
Independent-looking positions in one theme are one bet. Summing nominal per-position risk understates true exposure precisely when it matters (a correlated shock). **Absorbed by:** correlation/cluster-aware heat caps, not just a global heat number.

### 7.5 Stops can be skipped, gapped, or arbitrary — and that poisons fixed-fractional sizing
Fixed-fractional sizing is only as valid as the stop it's computed from. An arbitrary stop produces an arbitrary size; a gap through the stop produces a loss larger than the "1R" the system assumed (so realized −R can exceed −1). **Absorbed by:** requiring a defined risk basis per position (price stop, ATR multiple, or — for long options — premium-at-risk), logging *realized* R including overnight/gap slippage so expectancy isn't flattered, and (for options) using bounded premium-at-risk where no clean underlying stop exists.

### 7.6 The cheap-OTM-premium leverage trap
For Alex's specific book (long OTM calls/puts as directional leverage), the most dangerous failure is *not* losing too much on any single option — premium-at-risk caps that. It's that **sizing off premium-at-risk silently permits runaway notional/delta exposure**, because cheap OTM options buy huge effective share-equivalents per dollar. Five "small-risk" option positions can add up to a portfolio that is wildly net-long in delta terms. **Absorbed by:** making delta-adjusted notional (a leverage cap), not premium-at-risk, the *binding* per-position and portfolio constraint for the options leg — premium-at-risk feeds the heat/max-loss view, notional governs sizing. (Renumbering: the former 7.6 behavioral point becomes 7.7.)

### 7.7 The behavioral failure mode is abandonment, not blow-up
Your history says the real risk is that a tedious system gets dropped — at which point its theoretical optimality is worth zero. **Absorbed by:** the synthesized system is engineered around minimal entry and "faster than your current process," with banding/tiers to avoid constant re-sizing, smart defaults, and quick views. Usability is treated as a *first-class risk control*, not a nicety.

---

## 8. Synthesized system — a coherent sizing philosophy

The frameworks are not competitors; they are **layers**, each governing a different decision, each chosen for the part of it that survives the stress test. The system computes size by passing each candidate position through these layers in order:

**Layer 1 — Risk basis *and* exposure basis (per position).** Compute **two** numbers, not one: (a) dollar risk — stock/futures via entry-to-stop (or ATR multiple) × multiplier, long-options via premium-at-risk; and (b) delta-adjusted notional exposure (combined across stock + option legs on the same name). For Alex's long-OTM-options book these diverge sharply, so the system carries both throughout. *Origin: fixed-fractional + Van Tharp R; delta-additivity.*

**Layer 2 — Baseline size (fractional risk).** Size so that dollar-risk = *f* × silo equity, computed off **current** equity so it scales automatically. This is the transparent, robust chassis. *Origin: fixed-fractional.*

**Layer 3 — Volatility normalization.** Adjust so each position carries comparable risk per unit of its own volatility (ATR/σ), making positions commensurable across tickers and contracts and taming tail/drawdown behavior. Used for normalization, *not* market timing. *Origin: vol targeting, the replicable part.*

**Layer 4 — Growth-optimal target (fractional Kelly / optimal-f) as a ceiling, on a dimmer.** Use the historical R-distribution to estimate the growth-optimal fraction, then *deliberately* size at a fraction of it (¼–½ Kelly) and approach it gradually. This is the engine that tells an under-bettor "you can responsibly size larger here," while the fraction protects against estimation error and overbetting. *Origin: Kelly/optimal-f, disciplined by MacLean–Thorp–Ziemba.*

**Layer 5 — Account-scaling behavior.** Equity-linked fraction (auto-scales with growth), banded by tiers to avoid churn, with anti-martingale logic for adding to winners (each add smaller, stop advanced). This is the direct antidote to conservative drift. *Origin: account-scaling / anti-martingale.*

**Layer 6 — Two ceilings: portfolio heat *and* a leverage cap (the hard limits).** Cap (a) total open *risk* and per-cluster correlation-aware risk per silo (premium-at-risk + stop risk), and (b) total *delta-adjusted notional* leverage per silo — the latter is the binding constraint for the long-OTM-options book, where cheap premium can hide large net-long exposure (Section 7.6). Whatever Layers 2–5 propose, either ceiling can throttle it. This is what makes aggressive *per-trade* scaling safe at the *portfolio* level. *Origin: portfolio heat + correlation + options leverage management.*

**The evaluation engine (the payoff).** Independent of live sizing, replay historical trades as R-multiples and compute, per silo: realized expectancy and R-distribution; total P&L at actual size vs. at counterfactual sizing rules (e.g., mechanical fractional, ½-Kelly-capped, vol-normalized); and the **gap** between them — the explicit, dollar-denominated "alpha left on the table." This is the feature that proves the system's value and earns its daily use.

In one sentence: **a fixed-fractional chassis, volatility-normalized for comparability, pointed at a fractional-Kelly target it approaches gradually, auto-scaled to equity with anti-martingale add-ons, and hard-capped by both correlation-aware portfolio heat and a delta-notional leverage limit — with an R-multiple evaluation engine that continuously quantifies the cost of under-sizing.** Two silos, same philosophy, separate books; in the stocks+options silo the options are directional leverage (long OTM calls/puts), so notional/delta governs sizing while premium-at-risk governs max-loss.

---

## 9. Open decisions for Phase 2

These are the choices that will most shape the proposal. I'll bring recommendations for each, but your answers steer the design:

1. **Risk appetite anchor.** Where do you want the baseline per-trade fraction *f* to live (e.g., 0.5% / 1% / 1.5% / 2% of silo equity), what maximum portfolio heat per silo feels right (5%? 10%?), and — critically for the options book — what **maximum delta-notional leverage** per silo are you comfortable with (e.g., 1.5×, 2×, 3× of silo equity)? This sets the whole aggressiveness band.
2. **How aggressive a Kelly fraction.** Comfort with ¼-Kelly (very conservative bridge) vs. ½-Kelly (the classic) as the *target* the dimmer moves toward.
3. **Drawdown tolerance.** The single number — max peak-to-trough you're willing to endure — that calibrates Layers 4–6. (This is the input that makes the math concrete.)
4. **Stop discipline reality.** On the *stock* leg, do you always trade with a defined price stop, or manage discretionarily? (For the *options* leg I'll assume premium-at-risk is the max-loss basis since you hold long OTM with no underlying stop — confirm if that's right, and whether you ever cut options early on a thesis change vs. letting them ride to expiry.) This decides how the evaluation engine reconstructs realized R for each leg.
5. **Pyramiding.** Do you currently add to winners, and do you want the system to model/encourage anti-martingale adds, or just size the initial entry?
6. **Evaluation lookback & data availability.** How far back do your broker CSVs go (Robinhood individual + IRA, Schwab/thinkorswim, Tradovate), and is there a closed-trade history with entry/exit/stop we can mine for the R-distribution? The counterfactual engine is only as good as the history we can reconstruct.
7. **Stack preference signal.** Any early lean toward Excel-first (fastest adoption) vs. Python/Streamlit (deeper analysis) vs. lightweight web app? Not binding yet, but it colors the proposal.

---

## 10. Sources

**Foundational / academic**
- Kelly, J. L. (1956). *A New Interpretation of Information Rate.* Bell System Technical Journal. — origin of the Kelly criterion.
- Thorp, E. O. *The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market.* http://www.edwardothorp.com/wp-content/uploads/2016/11/TheKellyCriterionAndTheStockMarket.pdf
- Thorp, E. O. *Understanding the Kelly Criterion.* https://rybn.org/halloffame/PDFS/2008_Understanding_Kelly_New.pdf
- MacLean, L. C., Thorp, E. O., & Ziemba, W. T. *Good and Bad Properties of the Kelly Criterion.* https://www.stat.berkeley.edu/~aldous/157/Papers/Good_Bad_Kelly.pdf
- MacLean, Ziemba & Blazenko (1992), *Growth versus Security in Dynamic Investment Analysis*, Management Science. (Summarized via the MacLean–Thorp–Ziemba Kelly literature above.)
- Ziemba & MacLean, *Using the Kelly Criterion for Investing.* https://webhomes.maths.ed.ac.uk/mckinnon/blackouts/StochOptFinanceAndEnergySpringer/Chap1_KellyZiemba.pdf
- Moskowitz, T., Ooi, Y. H., & Pedersen, L. H. (2012). *Time Series Momentum*, Journal of Financial Economics. https://elmwealth.com/wp-content/uploads/2017/06/timeseriesmomentum.pdf
- *Time Series Momentum and Volatility Scaling* (Finance Research Letters / Journal of Financial Markets). https://www.sciencedirect.com/science/article/abs/pii/S1386418116301379 ; summary: https://alphaarchitect.com/time-series-momentum-volatility-scaling-and-crisis-alpha/
- Moreira, A., & Muir, T. (2017). *Volatility-Managed Portfolios*, Journal of Finance. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2659431 ; NBER WP: https://www.nber.org/system/files/working_papers/w22208/w22208.pdf
- *On the performance of volatility-managed portfolios* (counter-evidence), Journal of Financial Economics. https://www.sciencedirect.com/science/article/abs/pii/S0304405X2030132X
- *Conditional Volatility Targeting*, Financial Analysts Journal. https://www.tandfonline.com/doi/full/10.1080/0015198X.2020.1790853
- Brinson, Hood & Beebower (1986) and Brinson, Hood & Singer (1991), *Determinants of Portfolio Performance.* Critique: Jahnke (1997), *The Asset Allocation Hoax*, FPA Journal. https://www.financialplanningassociation.org/sites/default/files/2021-08/AUG04%20The%20Asset%20Allocation%20Hoax.pdf ; CFA Institute: https://blogs.cfainstitute.org/investor/2012/02/16/setting-the-record-straight-on-asset-allocation/

**Practitioner / applied**
- Van Tharp — R-multiples & position sizing. https://traderlion.com/risk-management/r-and-r-multiples/ ; https://vantharpinstitute.com/van-tharp-teaches-position-sizing-strategies-and-risk-management/
- Vince, R. *The Leverage Space Trading Model.* Wiley. https://onlinelibrary.wiley.com/doi/book/10.1002/9781119198314 ; overview: https://www.raynergobran.com/2011/04/leverage-space-trading-model/ ; Optimal-f: https://www.quantifiedstrategies.com/optimal-f-money-management/
- Fixed-fractional sizing references. https://www.adaptrade.com/MSA/fixfrac.htm ; https://www.quantifiedstrategies.com/fixed-fractional-position-sizing/
- Beta-weighted delta & directional options exposure. https://www.theoptionpremium.com/p/beta-weighted-delta-options-portfolio-risk-management ; https://www.macroption.com/option-delta-measuring-directional-exposure/ ; Cboe beta-weighting with XSP: https://www.cboe.com/insights/posts/how-to-right-size-hedges-via-beta-weighting-with-xsp-options/
- Long-OTM options as leverage; premium-at-risk vs. notional exposure. https://macro-ops.com/how-to-20x-your-money-without-upping-your-risk/ ; sizing by notional exposure: https://inthemoneybyzerodha.substack.com/p/position-sizing-part-3-sizing-option ; https://groww.in/blog/notional-exposure-and-leverage
- Skewed long-option payoff & Kelly for skewed payoffs. https://greekslab.com/blog/applying-the-kelly-criterion-to-0dte-options-trading ; probabilities of positive option returns: https://arxiv.org/pdf/0912.4973
- Futures: notional vs margin & ATR sizing. https://highstrike.com/notional-value-of-futures/ ; https://learn.optimusfutures.com/position-sizing ; https://www.quantifiedstrategies.com/volatility-based-position-sizing/
- Anti-martingale / pyramiding. https://quantstrategy.io/blog/pyramiding-strategies-how-to-safely-add-to-winning-trades/ ; https://ungeracademy.com/blog/pyramiding-and-scaling-out
- Portfolio heat & correlation. https://www.babypips.com/learn/forex/portfolio-heat-managing-total-risk-exposure ; https://www.tradingsprout.com/resources/portfolio-heat-management
- Fractional Kelly in practice. https://medium.com/@tmapendembe_28659/the-dangers-of-full-kelly-criterion-why-most-traders-should-use-fractional-kelly-criterion-instead-0338e3bcc705

---

*End of Phase 1. Awaiting your go-ahead to proceed to Phase 2 (proposal).*
