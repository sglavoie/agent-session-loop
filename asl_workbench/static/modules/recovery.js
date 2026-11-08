/* Recovery Center loading, and the conversation-recovery group.
 *
 * The compact recovery projection on a queue row renders first and the full
 * `/api/recovery` read replaces it, so the panel is populated immediately and
 * accurate a moment later. Conversation recovery is reported and never
 * performed: it renders the decision and the commands, and offers no button
 * that carries either out.
 *
 * The action cards are a second door onto controls the bar already offers, so
 * they are labelled with the same action names and put through the same
 * availability pass. A recovery card is read by someone who has just been told
 * something went wrong; offering them a button that this deployment cannot
 * carry out, and only saying so after they press it, is the worst place in the
 * interface to make that mistake.
 */

import { apiRequest, describeError } from "./api.js";
import { planReadGuard, planUrl, routePlanId } from "./identity.js";
import { hideReplyPanel, loadAndRenderReply } from "./panels.js";
import { state } from "./state.js";
import {
  loadAndRenderConversationRecovery,
  hideConversationRecoveryPanel,
} from "./recovery_conversation.js";
import { renderRecoveryError, renderRecoveryPanel } from "./recovery_render.js";

/** Whether the remote detail read names an unanswered question for this run.
 *
 * A live `asl ask --wait` is still a running turn, so its recovery card is not
 * `reply-needed`.  The remote plan read carries that question on its run row;
 * use the run key from the notification route when there is one so a retained
 * older run cannot make the current plan look answerable.
 */
export function hasLiveQuestion(runs, linkedRunKey, linkedQuestionId) {
  const rows = Array.isArray(runs) ? runs : [];
  return rows.some((run) => {
    if (!run || typeof run !== "object") return false;
    if (linkedRunKey && run.run_key !== linkedRunKey) return false;
    if (linkedQuestionId && run.question_id !== linkedQuestionId) return false;
    return (
      typeof run.question_id === "string" &&
      run.question_id.length > 0 &&
      Number(run.unanswered_count) > 0
    );
  });
}

export async function loadAndRenderRecovery(
  plan,
  runs,
  linkedRunKey,
  linkedQuestionId,
) {
  const planId = routePlanId();
  const stale = planReadGuard();
  const compact = plan && plan.recovery ? plan.recovery : null;
  let renderedRecovery = null;
  if (compact) {
    renderedRecovery = compact;
    // The compact projection omits evidence and artifacts. Replacing an
    // already populated panel with it on every poll collapses the detail
    // layout until the full recovery request completes.
    if (state.recoveryPanelPlanId !== planId) {
      renderRecoveryPanel(compact);
      state.recoveryPanelPlanId = planId;
    }
  }
  try {
    const body = await apiRequest(
      planUrl("/api/recovery")
    );
    if (stale()) return;
    if (body && body.ok === true && body.recovery) {
      renderedRecovery = body.recovery;
      renderRecoveryPanel(body.recovery);
      state.recoveryPanelPlanId = planId;
    } else if (!compact) {
      renderRecoveryError(
        (body && body.error) || "recovery diagnosis unavailable"
      );
      renderedRecovery = null;
    }
  } catch (err) {
    if (stale()) return;
    if (compact) {
      const fallback = Object.assign({}, compact, {
        warnings: [
          "Recovery details failed to load: " + describeError(err),
        ],
      });
      renderedRecovery = fallback;
      renderRecoveryPanel(fallback);
    } else {
      renderRecoveryError(describeError(err));
      renderedRecovery = null;
    }
  }
  if (
    linkedQuestionId ||
    (renderedRecovery && renderedRecovery.kind === "reply-needed") ||
    hasLiveQuestion(runs, linkedRunKey, linkedQuestionId)
  ) {
    await loadAndRenderReply();
  } else {
    hideReplyPanel();
  }
  if (renderedRecovery && renderedRecovery.kind === "conversation-recovery") {
    await loadAndRenderConversationRecovery();
  } else {
    hideConversationRecoveryPanel();
  }
}

export {
  loadAndRenderConversationRecovery,
  hideConversationRecoveryPanel,
  renderConversationRecoveryError,
  renderConversationRecovery,
  conversationRecoveryCommands,
  renderConversationRetained,
  conversationFactRow,
  emptyListItem,
} from "./recovery_conversation.js";
export {
  renderRecoveryError,
  renderRecoveryPanel,
  recoverySeverity,
  renderRecoveryActions,
  renderRecoveryPrimaryAction,
  createRecoveryActionCard,
  recoveryActionCanRun,
  recoveryActionButtonLabel,
  renderRecoveryEvidence,
  recoveryEvidenceSeverity,
  renderRecoveryCommands,
  renderRecoveryCommandRows,
  renderRecoveryArtifacts,
} from "./recovery_render.js";
export {
  createRecoveryLink,
  isRecoveryActionTarget,
  normalizeRecoveryActionTarget,
  hasExplicitRecoveryActionTarget,
  mappedRecoveryActionTarget,
  legacyRecoveryActionTarget,
  recoveryActionTarget,
  recoveryEvidenceTarget,
  recoveryArtifactTarget,
  recoveryText,
  handleRecoveryAction,
  focusDetailKey,
  copyRecoveryCommand,
  copyText,
} from "./recovery_targets.js";
