import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

def send_slack_alert(endpoint_id, signal_type, severity, score, raw_value,
                     correlation=None, deploy=None, explanation=None, ttd=None):
    """
    Envoie l'alerte FIRING sur Slack.

    `correlation` : dict retourne par correlator.correlate() (ou None) -- ajoute
                    la cause suspectee (scenario/fault + imputation_score).
    `deploy`      : dict retourne par correlator.correlate_deploy() (ou None) --
                    ajoute le deploiement suspecte (service/version + score).
    `explanation` : dict retourne par explainer.generate_explanation() (ou None)
                    -- ajoute summary / cause probable / verifications. Peut etre
                    un template de fallback (champ `fallback=True`).
    `ttd`         : dict retourne par ttd.estimate_ttd() (ou None) -- ajoute une
                    alerte precoce advisory (extrapolation de tendance vers le SLO).

    Tous les enrichissements sont optionnels : si l'un manque, l'alerte de base
    part quand meme (l'alerting ne depend jamais du LLM ni de la correlation).
    """
    if not SLACK_WEBHOOK_URL:
        log.warning("SLACK_WEBHOOK_URL not set, skipping notification")
        return

    if signal_type == "p99_ms":
        detail = f"p99={raw_value:.0f}ms (seuil depasse)"
    else:
        detail = f"error_rate={raw_value:.1f}%"

    emoji = "🔴" if severity == "critical" else "🟠"
    lines = [
        f"{emoji} *FIRING* [{severity.upper()}]",
        f"*Endpoint:* {endpoint_id}",
        f"*Signal:* {signal_type}",
        f"*Detail:* {detail}",
        f"*Score:* {score:.2f}",
    ]

    # Deploiement suspecte issu de la correlation deploy_events
    if deploy:
        version = deploy.get("version", "?")
        service = deploy.get("service", "?")
        imp = deploy.get("imputation_score")
        imp_txt = f" (score {imp:.2f})" if isinstance(imp, (int, float)) else ""
        lines.append(f"*Deploiement suspecte:* {service} {version}{imp_txt}")

    # Cause suspectee issue de la correlation d'injection (scenario ground-truth)
    if correlation:
        scenario = correlation.get("scenario_id", "?")
        fault = correlation.get("fault_type", "?")
        imp = correlation.get("imputation_score")
        imp_txt = f" (score {imp:.2f})" if isinstance(imp, (int, float)) else ""
        lines.append(f"*Cause suspectee:* {scenario} / {fault}{imp_txt}")

    # Alerte precoce TTD (advisory, extrapolation de tendance -- spec 8.4)
    if ttd and not ttd.get("already_breaching"):
        minutes = ttd.get("ttd_minutes")
        low, high = ttd.get("ttd_low"), ttd.get("ttd_high")
        if isinstance(minutes, (int, float)):
            rng = ""
            if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                rng = f" [{low:.0f}-{high:.0f}]"
            slope = ttd.get("slope_ms_per_min")
            slope_txt = f", +{slope:.1f}ms/min" if isinstance(slope, (int, float)) else ""
            lines.append(
                f"*Alerte precoce (tendance):* SLO p99 atteint dans ~{minutes:.0f} min{rng}"
                f"{slope_txt} _(extrapolation, advisory)_"
            )

    # Explication en langage naturel (LLM ou template de fallback)
    if explanation:
        src = "template" if explanation.get("fallback") else "LLM"
        summary = explanation.get("summary")
        cause = explanation.get("suspected_cause")
        checks = explanation.get("checks") or []
        if summary:
            lines.append(f"\n> {summary}")
        if cause:
            lines.append(f"*Cause probable ({src}):* {cause}")
        if checks:
            checks_txt = "\n".join(f"  • {c}" for c in checks)
            lines.append(f"*Verifications:*\n{checks_txt}")

    text = "\n".join(lines)
    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": text},
            timeout=5,
        )
        if resp.status_code != 200:
            log.error(f"Slack notification failed: {resp.status_code} {resp.text}")
        else:
            log.info(f"Slack notification sent for {endpoint_id} -> {signal_type}")
    except Exception as e:
        log.error(f"Slack notification error: {e}")
