"""Bind the reviewed consolidation ranking to measured source copies."""


def ranked_candidates(data):
    latest = {
        repo: next(s for s in reversed(data["snapshots"]) if s["repository"] == repo)
        for repo in ("reckon", "nova")
    }

    def copies(repo, kind, concept):
        census = latest[repo]["scopes"][repo + "/"]["reimplementation_census"]
        return next(
            c["copies"] for c in census if c["kind"] == kind and c["concept"] == concept
        )

    candidates = [
        {
            "title": "One UTC timestamp parser for reckon",
            "repository": "reckon",
            "copies": copies("reckon", "primitive", "ISO timestamp parsing"),
            "home": "reckon/_timestamps.py:parse_utc (new primitive module)",
            "reason": "44 functions call fromisoformat directly. Parsing affects quota windows, freshness and resume eligibility, so policy drift is more costly than these functions' size. Extract parsing only; elapsed-time and display policy remain with their callers.",
            "guard": "Specify malformed-input behavior, naive timestamps, Z suffixes, offsets and numeric epoch handling before migration. Some of these 44 are consumers with inline parsing, not interchangeable whole functions.",
        },
        {
            "title": "One atomic JSON persistence primitive for reckon",
            "repository": "reckon",
            "copies": copies("reckon", "primitive", "Atomic JSON file replacement"),
            "home": "reckon/_store.py:write_json_atomically (new primitive beside existing envelope writer)",
            "reason": "14 functions independently serialize JSON and replace a file. The existing writers differ in temporary naming, cleanup and fsync. One primitive would concentrate interrupted-write behavior and concurrency verification.",
            "guard": "Keep envelope construction, pointer launcher-host stamping and response validation outside the writer; preserve each caller's locking and durability requirements. The layout migration also moves non-JSON files and is not replaced wholesale.",
        },
        {
            "title": "One ordered enumeration of original and resumed streams",
            "repository": "reckon",
            "copies": copies("reckon", "same_name", "run_streams"),
            "home": "reckon/crew/metering.py:run_streams (existing implementation)",
            "reason": "Three implementations select the original stream and numerically ordered resume files. Reuse prevents billing and promotion from disagreeing about which attempts exist; the metering implementation already handles an empty path.",
            "guard": "Adapt the ledger's run-id/root input at its boundary; retain numeric resume ordering and missing-file behavior. Do not replace schema-specific stream interpretation with one undocumented parser.",
        },
        {
            "title": "Share the algebra common to equilibrium constraints",
            "repository": "nova",
            "copies": copies(
                "nova",
                "identical_body",
                "d50ba84c2b89f41cbeda537b5a57610554940d0cf9c977336b64befc7ca9b97c",
            )
            + copies(
                "nova",
                "identical_body",
                "fd56b7b9eddc67ae3d28dc596e014c112e56398c3a0a24cdcb9f8d55def91450",
            ),
            "home": "nova/equilibrium/constraint.py (shared residual and dual-image implementation)",
            "reason": "Seven residual methods have identical AST bodies, and five dual-flux-image methods form a second identical group. Their observed-minus-target scaling and Jacobian-axis conventions are algebra that should have one tested implementation.",
            "guard": "Keep each constraint's observed quantity, payload and registration distinct. Preserve JAX tracing and array axis behavior; a common protocol alone does not share implementation.",
        },
        {
            "title": "Share axis validation mechanics with explicit precision policy",
            "repository": "nova",
            "copies": copies("nova", "same_name", "uniform_axis"),
            "home": "nova/utilities/axes.py:uniform_axis (new common validator)",
            "reason": "Three functions independently validate increasing uniform axes. The media readers reconstruct endpoint-preserving axes, while map extraction requires at least three points and strict spacing. Shared mechanics can make those policy choices visible.",
            "guard": "Do not force identical tolerances: MAST uses stored dtype epsilon, DIII-D uses an absolute coordinate-scaled tolerance, and map extraction uses relative spacing tolerance. Preserve minimum sizes, return shapes and dtype behavior.",
        },
        {
            "title": "One owned immutable-array constructor",
            "repository": "nova",
            "copies": copies("nova", "same_name", "readonly"),
            "home": "nova/utilities/arrays.py:readonly_copy (new common array primitive)",
            "reason": "Three helpers independently copy an array and clear its write flag. Sharing the ownership boundary makes accidental aliasing testable in one place.",
            "guard": "Retain dtype=None versus dtype=float defaults at the call sites and prove that changing the original input cannot change the returned array. The wider primitive census has additional inlined clients, not all equivalent constructors.",
        },
        {
            "title": "One bool-rejecting numeric observation decoder",
            "repository": "reckon",
            "copies": copies(
                "reckon",
                "identical_body",
                "7a4418be30ac71a079c41b3990fef80107ac5473fabb62e26df7ace17eca2358",
            ),
            "home": "reckon/_observations.py:optional_number (new observation primitive)",
            "reason": "Four differently named functions have exactly the same body: reject bool and non-int/float values, otherwise return float(value). This is a low-coupling consolidation with a clear contract.",
            "guard": "Do not silently add finiteness or string coercion; neighboring numeric decoders have different semantics. Both None and non-finite numeric values need explicit expectations.",
        },
        {
            "title": "One atomic JSON writer for nova's durable receipts",
            "repository": "nova",
            "copies": copies("nova", "primitive", "Atomic JSON file replacement"),
            "home": "nova/io/json.py:write_atomic (new shared writer)",
            "reason": "Three writers independently stage and replace chunk, fingerprint and scorecard files. Shared mechanics can give each a unique sibling temporary and consistent cleanup while retaining its payload.",
            "guard": "Preserve strict JSON sanitization, indentation and digest-sensitive serialization. These are file receipts, not IMAS data access. A dataclass replace call was explicitly excluded by the instrument control.",
        },
        {
            "title": "Extend the already shared contract validation with trimmed text",
            "repository": "nova",
            "copies": copies("nova", "same_name", "trimmed"),
            "home": "nova/imas/machine_evidence.py:require_trimmed_string (alongside require_string)",
            "reason": "Three helpers repeat non-empty, whitespace-trimmed text checks after calling the same require_string primitive. The existing cross-module dependency provides a natural home without a new abstraction family.",
            "guard": "Accept the caller's exception type and context so SourceMapError, DriveError and EvidenceError retain their current meaning.",
        },
        {
            "title": "One digest operation over canonical contract bytes",
            "repository": "nova",
            "copies": copies(
                "nova",
                "identical_body",
                "d69b93f5014c568415f20d2274cb7689242a42793967dc27f5d02cc3bc1fa6db",
            ),
            "home": "nova/imas/machine_evidence.py:canonical_digest (beside canonical_json)",
            "reason": "Three content-addressed contracts hash canonical_bytes with SHA-256 and truncate to 16 hex characters. The shared canonical JSON layer is the appropriate home for the byte-level digest convention.",
            "guard": "Pass the existing canonical bytes unchanged and retain the 16-character length. Do not substitute catalog serialization just because it also hashes JSON; byte identity is the contract.",
        },
    ]
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank
        candidate["commit"] = latest[candidate["repository"]]["commit"]
    return candidates
