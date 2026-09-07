# Cyrene Product Services Catalog

Cyrene organizes functional capabilities into specialized, decoupled product repositories. Each product maintains its own clean-room test suite, independent CI pipeline, and service contract.

---

## 1. Cyrene-Catalyst (Data Engineering & Curation)
- **Repo**: `DoHorizon-AI/Cyrene-Catalyst`
- **Primary Responsibility**:
  - Raw dataset ingestion, cleaning, tokenization, and schema validation.
  - Generates immutable `DatasetVersion` artifacts.
  - Enforces dataset provenance, licensing compliance, and minimal representative samples in public repositories.
- **Key Schemas / Artifacts**:
  - `DatasetVersion` (`dataset-version://...`)

---

## 2. Cyrene-Yield (Training & Fine-Tuning Engine)
- **Repo**: `DoHorizon-AI/Cyrene-Yield`
- **Primary Responsibility**:
  - Executes distributed training runs, LoRA adaptation, and full fine-tuning.
  - Consumes `DatasetVersion` from Catalyst.
  - **Single Authority** for creating, hashing, and persisting canonical `ModelVersion` artifacts (`model-version://sha256/<hash>`).
- **Key Schemas / Artifacts**:
  - `ModelVersion`
  - `TrainingRun`

---

## 3. Cyrene-Reactor (Inference Serving Engine)
- **Repo**: `DoHorizon-AI/Cyrene-Reactor`
- **Primary Responsibility**:
  - Production inference serving with optimized execution backends (vLLM, SGLang, TensorRT-LLM, HuggingFace fallback).
  - High-performance Rust core (`cy_exec`, `cy_exec_pro`) with Python integration layer.
  - Hot model swapping based on verified `ModelVersion` manifests.
- **Key Interfaces**:
  - OpenAI-compatible `/v1/chat/completions` and `/v1/completions`.
  - Internal high-speed IPC/gRPC execution endpoints.

---

## 4. Cyrene-Exchange (API Gateway & Governance)
- **Repo**: `DoHorizon-AI/Cyrene-Exchange`
- **Primary Responsibility**:
  - Unified entry point for API traffic, client routing, and policy enforcement.
  - Complete multi-tenant domain models: API key management, quota tracking, billing records, and audit logging.
  - Load balancing across multiple Reactor serving instances.
- **Domain Modules**:
  - `cyrene_exchange.governance` (`APIKey`, `TenantQuota`, `UsageRecord`, `BillingRecord`, `AuditLog`).

---

## 5. Cyrene-Navigator (Desktop Orchestrator & Client UI)
- **Repo**: `DoHorizon-AI/Cyrene-Navigator`
- **Primary Responsibility**:
  - Front-end user interface and desktop integration.
  - Local session orchestration and capability dispatch.
  - Connects to Exchange for inference and Echo for feedback submission.

---

## 6. Cyrene-Echo (Feedback, Evaluation & Analytics)
- **Repo**: `DoHorizon-AI/Cyrene-Echo`
- **Primary Responsibility**:
  - Collects user ratings, corrections, and execution feedback from Navigator sessions.
  - Computes evaluation metrics (perplexity, coherence, safety guardrails, token efficiency).
  - Stores canonical analytical models (`SessionTokenUsage`, `SessionCost`, `ContextBreakdown`, `CapabilityDowngradeDisplay`).
  - Exports validated insights to Catalyst for dataset reinforcement loops.
