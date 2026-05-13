"""
Analyzes captured session data for token inefficiencies and produces
actionable improvement suggestions with estimated savings.
"""

from collections import Counter, defaultdict


# ── Per-session analysis ──────────────────────────────────────────────────────

def analyze_session(session: dict, api_calls: list, tool_calls: list) -> list:
    """Return a list of issue dicts for a single session."""
    issues = []

    total_input = (session.get("total_input_tokens") or 0) + (session.get("total_cache_read_tokens") or 0)
    cache_read  = session.get("total_cache_read_tokens") or 0
    n_calls     = len(api_calls)

    # 1. Low cache utilization
    if total_input > 5_000 and cache_read / total_input < 0.15:
        issues.append({
            "type": "low_cache",
            "severity": "high",
            "title": "Low cache hit rate",
            "detail": f"Only {cache_read/total_input*100:.1f}% of input tokens came from cache. "
                      f"You're paying full price for context that could be cached.",
        })

    # 2. Context bloat across calls
    if n_calls >= 3:
        first = api_calls[0].get("input_tokens") or 0
        last  = api_calls[-1].get("input_tokens") or 0
        if first > 0 and last > first * 3:
            issues.append({
                "type": "context_bloat",
                "severity": "high",
                "title": "Context window bloat",
                "detail": f"Input token count grew {last/first:.1f}× during the session "
                          f"({first:,} → {last:,}). Large tool outputs are accumulating in context.",
            })

    # 3. Tool call loops
    tool_name_counts = Counter(t.get("tool_name", "") for t in tool_calls)
    for tool_name, count in tool_name_counts.most_common(3):
        if count >= 5 and tool_name:
            issues.append({
                "type": "tool_loop",
                "severity": "medium",
                "title": f"Repeated tool: {tool_name}",
                "detail": f"'{tool_name}' was called {count} times in this session. "
                          f"This pattern often indicates iterative trial-and-error.",
            })

    # 4. Large tool responses
    large = [t for t in tool_calls if (t.get("response_size") or 0) > 50_000]
    if large:
        avg_kb = sum(t.get("response_size", 0) for t in large) / len(large) / 1024
        issues.append({
            "type": "large_response",
            "severity": "medium",
            "title": f"{len(large)} oversized tool responses",
            "detail": f"{len(large)} tool calls returned >50 KB each (avg {avg_kb:.0f} KB). "
                      f"These responses are fed back into the context window.",
        })

    # 5. No cache at all
    if (session.get("total_cache_creation_tokens") or 0) == 0 and total_input > 10_000:
        issues.append({
            "type": "no_caching",
            "severity": "medium",
            "title": "No prompt cache seeded",
            "detail": "Zero cache-creation tokens means no cache was written this session. "
                      "Without a CLAUDE.md or consistent system prompt, nothing is cacheable.",
        })

    # 6. High output ratio
    out   = session.get("total_output_tokens") or 0
    inp   = session.get("total_input_tokens") or 0
    total = inp + out
    if total > 5_000 and out / total > 0.35:
        issues.append({
            "type": "high_output_ratio",
            "severity": "low",
            "title": "High output token ratio",
            "detail": f"Output tokens are {out/total*100:.0f}% of total. "
                      f"Claude may be generating more verbose responses than necessary.",
        })

    return issues


# ── Global analysis ───────────────────────────────────────────────────────────

def analyze_all(sessions: list, api_calls: list, tool_calls: list) -> dict:
    if not sessions:
        return {
            "issues": [],
            "suggestions": [],
            "score": {"overall": 100, "caching": 100, "context": 100, "tool_efficiency": 100},
            "summary": {"total_sessions": 0, "total_tokens": 0, "estimated_waste_tokens": 0,
                        "estimated_waste_cost": 0.0, "potential_savings_pct": 0},
        }

    # Index by session_id
    calls_by_session = defaultdict(list)
    for c in api_calls:
        calls_by_session[c["session_id"]].append(c)

    tools_by_session = defaultdict(list)
    for t in tool_calls:
        tools_by_session[t["session_id"]].append(t)

    # Run per-session analysis
    all_issues_by_type = Counter()
    per_session_issues = {}
    for s in sessions:
        sid = s["id"]
        issues = analyze_session(s, calls_by_session[sid], tools_by_session[sid])
        per_session_issues[sid] = issues
        for iss in issues:
            all_issues_by_type[iss["type"]] += 1

    # Aggregate token totals
    total_input         = sum(s.get("total_input_tokens", 0)          or 0 for s in sessions)
    total_output        = sum(s.get("total_output_tokens", 0)         or 0 for s in sessions)
    total_cache_create  = sum(s.get("total_cache_creation_tokens", 0) or 0 for s in sessions)
    total_cache_read    = sum(s.get("total_cache_read_tokens", 0)     or 0 for s in sessions)
    total_cost          = sum(s.get("estimated_cost_usd", 0)          or 0 for s in sessions)
    effective_input     = total_input + total_cache_read

    # Cache efficiency score (0–100)
    cache_efficiency = (total_cache_read / effective_input * 100) if effective_input > 0 else 0
    cache_score = min(100, int(cache_efficiency * 100 / 40))  # 40% cache rate → score 100

    # Context efficiency: penalize sessions with context_bloat
    bloat_sessions = sum(1 for issues in per_session_issues.values()
                         if any(i["type"] == "context_bloat" for i in issues))
    context_score = max(0, 100 - int(bloat_sessions / max(len(sessions), 1) * 100))

    # Tool efficiency: penalize tool_loop + large_response
    bad_tool_sessions = sum(1 for issues in per_session_issues.values()
                            if any(i["type"] in ("tool_loop", "large_response") for i in issues))
    tool_score = max(0, 100 - int(bad_tool_sessions / max(len(sessions), 1) * 100))

    overall_score = int((cache_score * 0.4) + (context_score * 0.35) + (tool_score * 0.25))

    # Estimate wasted tokens (could-be-cached input that wasn't)
    wasteable_input = max(0, total_input - total_cache_create)
    potential_cache_savings = int(wasteable_input * 0.7)  # 70% could realistically be cached
    # Cache read costs ~90% less than input, so the "waste" is the price difference
    SONNET_INPUT  = 3.0 / 1e6
    SONNET_CACHE  = 0.30 / 1e6
    estimated_waste_cost = potential_cache_savings * (SONNET_INPUT - SONNET_CACHE)
    potential_savings_pct = int(estimated_waste_cost / total_cost * 100) if total_cost > 0 else 0

    # Build global issue list (deduplicated, most-impactful first)
    global_issues = []

    if all_issues_by_type.get("low_cache", 0) + all_issues_by_type.get("no_caching", 0) > 0:
        affected = all_issues_by_type.get("low_cache", 0) + all_issues_by_type.get("no_caching", 0)
        global_issues.append({
            "type": "low_cache",
            "severity": "high",
            "title": "Prompt caching underutilized",
            "detail": (f"Across {affected} of {len(sessions)} sessions, cache hit rate is below 15%. "
                       f"You could save ~${estimated_waste_cost:.2f} by enabling caching."),
            "affected_sessions": affected,
        })

    if all_issues_by_type.get("context_bloat", 0) > 0:
        global_issues.append({
            "type": "context_bloat",
            "severity": "high",
            "title": "Context window bloat detected",
            "detail": (f"{all_issues_by_type['context_bloat']} sessions showed >3× context growth. "
                       f"Tool outputs are accumulating and inflating every subsequent API call."),
            "affected_sessions": all_issues_by_type["context_bloat"],
        })

    if all_issues_by_type.get("large_response", 0) > 0:
        global_issues.append({
            "type": "large_response",
            "severity": "medium",
            "title": "Oversized tool responses",
            "detail": (f"{all_issues_by_type['large_response']} sessions had tool calls returning >50 KB. "
                       f"Large Bash/Read outputs inflate context on every subsequent call."),
            "affected_sessions": all_issues_by_type["large_response"],
        })

    if all_issues_by_type.get("tool_loop", 0) > 0:
        global_issues.append({
            "type": "tool_loop",
            "severity": "medium",
            "title": "Repeated tool call patterns",
            "detail": (f"{all_issues_by_type['tool_loop']} sessions had tools called 5+ times. "
                       f"This may signal iterative trial-and-error that could be batched."),
            "affected_sessions": all_issues_by_type["tool_loop"],
        })

    # Top tools by total response size
    tool_stats = Counter()
    for t in tool_calls:
        tool_stats[t.get("tool_name", "unknown")] += t.get("response_size", 0)

    most_verbose_tools = [{"tool": k, "total_bytes": v}
                          for k, v in tool_stats.most_common(5)]

    # Suggestions
    suggestions = _build_suggestions(
        cache_score, context_score, tool_score,
        effective_input, total_cache_read, most_verbose_tools,
        potential_savings_pct, estimated_waste_cost
    )

    return {
        "issues": global_issues,
        "suggestions": suggestions,
        "score": {
            "overall":         overall_score,
            "caching":         cache_score,
            "context":         context_score,
            "tool_efficiency": tool_score,
        },
        "summary": {
            "total_sessions":         len(sessions),
            "total_tokens":           effective_input + total_output,
            "total_input_tokens":     total_input,
            "total_output_tokens":    total_output,
            "total_cache_read":       total_cache_read,
            "total_cache_creation":   total_cache_create,
            "total_cost":             round(total_cost, 4),
            "estimated_waste_tokens": potential_cache_savings,
            "estimated_waste_cost":   round(estimated_waste_cost, 4),
            "potential_savings_pct":  potential_savings_pct,
            "cache_hit_rate_pct":     round(cache_efficiency, 1),
        },
        "most_verbose_tools": most_verbose_tools,
    }


def _build_suggestions(cache_score, context_score, tool_score,
                        effective_input, cache_read,
                        most_verbose_tools, savings_pct, savings_cost) -> list:
    suggestions = []

    if cache_score < 60:
        suggestions.append({
            "category": "Caching",
            "priority": "high",
            "title": "Add a CLAUDE.md to each project root",
            "description": (
                "Claude Code reads CLAUDE.md as a system prompt. A consistent system prompt "
                "lets Anthropic cache it across all calls in a session, turning expensive "
                "input tokens into cheap cache-read tokens (90% cheaper)."
            ),
            "estimated_savings": f"~{savings_pct}% cost reduction (~${savings_cost:.2f} saved so far)",
            "how_to": "Run `claude /init` inside any project to generate a CLAUDE.md automatically.",
        })
        suggestions.append({
            "category": "Caching",
            "priority": "high",
            "title": "Keep sessions shorter and more focused",
            "description": (
                "Cache entries are created at the end of a cached prefix. Long rambling "
                "sessions constantly invalidate the cache boundary. Focused sessions "
                "(one task per session) cache more aggressively."
            ),
            "estimated_savings": "Up to 40% reduction in input token costs",
            "how_to": "Start a new `claude` invocation for each distinct task instead of doing everything in one session.",
        })

    if context_score < 70:
        suggestions.append({
            "category": "Context Management",
            "priority": "high",
            "title": "Truncate large Bash outputs",
            "description": (
                "Every byte a tool returns gets appended to the conversation history "
                "and re-sent with every subsequent API call. Truncating tool outputs "
                "at the source dramatically reduces context growth."
            ),
            "estimated_savings": "30–60% reduction in later-call input tokens",
            "how_to": (
                "Pipe Bash outputs: `| head -100`, `| tail -50`, `| grep pattern`. "
                "For file reads, use line ranges: Read with offset/limit parameters."
            ),
        })
        suggestions.append({
            "category": "Context Management",
            "priority": "medium",
            "title": "Use /compact periodically in long sessions",
            "description": (
                "Claude Code's /compact command summarizes the conversation history, "
                "replacing verbose tool outputs with a compact summary. This resets "
                "context bloat without losing the thread of work."
            ),
            "estimated_savings": "50–70% reduction in input tokens for calls after /compact",
            "how_to": "Type `/compact` in the Claude Code prompt mid-session when context feels heavy.",
        })

    if tool_score < 70 and most_verbose_tools:
        top_tool = most_verbose_tools[0]["tool"] if most_verbose_tools else "Bash"
        suggestions.append({
            "category": "Tool Efficiency",
            "priority": "medium",
            "title": f"Reduce '{top_tool}' output verbosity",
            "description": (
                f"'{top_tool}' is your most verbose tool by total bytes returned. "
                f"Large responses immediately inflate context for all subsequent calls."
            ),
            "estimated_savings": "10–30% reduction in total input tokens",
            "how_to": (
                "Use grep, head, tail, jq, or awk to filter outputs. "
                "Ask Claude to read specific line ranges instead of entire files."
            ),
        })

    suggestions.append({
        "category": "Cost Optimization",
        "priority": "low",
        "title": "Use Haiku for simple sub-tasks",
        "description": (
            "Claude Haiku 4.5 is 4× cheaper for input and 4× cheaper for output than Sonnet. "
            "Simple tasks (file listing, grep, formatting) don't need Sonnet-level intelligence."
        ),
        "estimated_savings": "Up to 75% cost reduction on simple tasks",
        "how_to": "Claude Code supports model selection per-task. Check /model or settings.",
    })

    suggestions.append({
        "category": "Workflow",
        "priority": "low",
        "title": "Batch related file operations",
        "description": (
            "Reading 10 files one-by-one generates 10 tool round-trips, "
            "each adding to context. Reading files in parallel (multiple tool calls per turn) "
            "reduces the number of API calls and total context growth."
        ),
        "estimated_savings": "Fewer API calls = fewer round-trip costs",
        "how_to": "Claude Code already batches independent tool calls. Phrase requests to allow parallelism.",
    })

    return suggestions
