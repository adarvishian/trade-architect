**PROJECT CONTEXT — Position Sizing Optimization & Trade Evaluation System**

**Core Objective**  
Build a robust, extremely easy-to-use system that helps me optimize position sizing on directional trend/momentum trades so I stop leaving meaningful gains on the table as my account equity grows. The system must track positions, ingest data from my brokers with minimal effort, and provide clear evaluation of my historical sizing decisions.

**Position Definitions (Source of Truth)**
- **Stocks + Options Positions**: A single logical "position" represents the blended directional exposure for one specific ticker. This combines stock shares with directional options (calls or puts) on the same underlying. The system should track the components but allow analysis and sizing at the unified position level.
- **Futures Positions**: Completely separate silo from the stock/options book. Positions are tracked per futures symbol/contract with their own margin and P&L characteristics. No blending across asset classes.

**User Trading Reality**
- Style: Purely directional (trend and momentum). I handle all thesis, entry, and exit decisions.
- Pain point: Current sizing is intuitive but increasingly conservative or inconsistent as account size grows → significant alpha left on the table.
- Non-requirements: No live data, no signal generation, no "how to trade" advice.

**Data Ingestion Requirements**
- Primary method: CSV exports from Robinhood (individual + IRA), Schwab/thinkorswim, and Tradovate.
- Secondary: Direct API connections where they are simple and low-maintenance (no persistent live connections needed).
- Goal: Fast normalization of positions across brokers into one unified view with minimal manual work.

**Usability Mandate (Non-Negotiable)**
This system must feel faster and less tedious than my current process. Any solution that requires heavy manual logging will fail like previous attempts. Prioritize speed, smart defaults, quick views, and minimal friction above all else.

**Evaluation & Optimization Focus**
- Quantify missed returns from previous sizing decisions.
- Provide transparent, explainable sizing frameworks that scale intelligently with account growth.
- Track metrics that matter: realized P&L attribution to size, potential additional returns under better sizing rules, risk-adjusted impact, and clear before/after comparison.

**Technical Flexibility**
- Acceptable stacks: Excel-first (for speed of adoption), Python (pandas + Streamlit/CLI for power), or lightweight web app if it dramatically improves daily use.
- Must remain simple, local/self-contained where possible, and auditable.

**Universal Constraints (Never Violate)**
- Never suggest trades, directions, or market views.
- Never create tedious data entry workflows.
- Always keep futures and stock/options siloed.
- All sizing logic and evaluation must be transparent and user-controllable.
- Favor 80/20 solutions that deliver high value with low ongoing effort.

**Success Criteria**
I will actually use this system consistently because it is faster than my current method and clearly shows me how to size positions more effectively while quantifying the improvement in captured returns.