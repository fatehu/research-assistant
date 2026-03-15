# Academic Papers for Generative UI

## Source list

### 1. Generative AI in Multimodal User Interfaces: Trends, Challenges, and Cross-Platform Adaptability

- Link: <https://arxiv.org/abs/2411.10234>
- Why it matters:
  - strong high-level survey
  - useful for understanding multimodal UI tradeoffs
  - emphasizes the "interface dilemma" for LLM systems

### 2. Towards a Working Definition of Designing Generative User Interfaces

- Link: <https://arxiv.org/abs/2505.15049>
- Why it matters:
  - frames generative UI as iterative, co-creative, and curation-heavy
  - useful for avoiding the trap of "AI makes page, humans accept it blindly"

### 3. Frontend Diffusion: Exploring Intent-Based User Interfaces through Abstract-to-Detailed Task Transitions

- Link: <https://arxiv.org/abs/2408.00778>
- Why it matters:
  - useful reference for staged transition from abstract intent to structured output
  - reinforces the value of multi-stage runtime design

### 4. Magentic-UI: Towards Human-in-the-loop Agentic Systems

- Link: <https://arxiv.org/abs/2507.22358>
- Why it matters:
  - connects agentic UI with human oversight instead of one-shot autonomy
  - explicitly combines tools, multi-agent runtime, and UI mechanisms
  - is highly relevant to `/workbench` and runtime inspection

### 5. Towards a Working Definition of Designing Generative User Interfaces

- Link: <https://arxiv.org/abs/2505.15049>
- Why it matters:
  - frames generative UI as curation-heavy and collaborative rather than "model outputs perfect interface"
  - useful for product and evaluation thinking, not just implementation

### 6. Generative Interfaces for Language Models

- Link: <https://arxiv.org/abs/2508.19227>
- Why it matters:
  - treats LLM responses as proactively generated interfaces instead of plain text
  - useful for reasoning about page-level adaptation rather than card-level augmentation

### 7. DuetUI: A Bidirectional Context Loop for Human-Agent Co-Generation of Task-Oriented Interfaces

- Link: <https://arxiv.org/abs/2509.13444>
- Why it matters:
  - emphasizes the value of iterative refinement loops instead of one-shot layout generation
  - relevant to future `/experience` interaction and incremental page patching

## Distilled implications

### 1. Generative UI is not the same thing as code generation

All three references are useful here:

- the hard part is not raw rendering
- the hard part is intent transition, curation, grounding, and usable interaction

Implication for this repo:

- `/experience` should not become "model writes frontend"
- it should become "model plans and curates a page over trusted primitives"

### 2. Curation matters as much as generation

The strongest pattern across the papers:

- AI generation must be constrained by selection, filtering, and refinement

Implication for this repo:

- staged runtime is the right next move
- agent should not dump raw ideas directly into the page

### 3. Multimodal context changes page generation quality

Implication for this repo:

- adjacent-page `VL-flash` structure is not optional decoration
- image/table/equation descriptions should meaningfully shape page generation

### 4. Evaluation is part of the architecture

Implication for this repo:

- `/experience` is not production-grade until evals, telemetry, and review loops are stronger

### 5. Human involvement is a feature, not an implementation accident

Across the newer papers, one pattern keeps recurring:

- users need visibility
- users need steering
- users need interruption / correction mechanisms

Implication for this repo:

- `/workbench` is not optional scaffolding
- it is part of how this system becomes trustworthy and debuggable

### 6. Staging beats one-shot generation

The research direction keeps reinforcing that good generative interfaces emerge from:

- intent distillation
- structured refinement
- constrained rendering

Implication for this repo:

- staged runtime is not engineering overkill
- it is the most defensible next step for `/experience`

## How to use these papers

- Use them to shape architecture and evaluation.
- Do not treat them as implementation specs.
- Prefer official protocol/framework docs when making concrete runtime decisions.
