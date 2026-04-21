/**
 * RightPanelContext — shared "which right sheet is open?" state.
 *
 * The assistant page has two mutually-exclusive right-side sheets:
 *   - "artifacts"  — the generated files panel (ArtifactsPanel)
 *   - "activity"   — the Claude-design Activity drawer (ActivityPanel)
 *
 * Never both at once. This context lets deep descendants (e.g. the
 * ActivityPill inside a ChatMessage) request the panel change without
 * prop-drilling; the page owns the actual state.
 *
 * The page component is also the only writer to `setShowArtifacts` in
 * useChatSession, so we mirror artifacts open/close here.
 */

import { createContext, useContext } from "react";

export type RightPanel = "activity" | "artifacts" | null;

export interface RightPanelState {
  rightPanel: RightPanel;
  /** Message ID whose activity is currently displayed (when rightPanel==="activity"). */
  activityMessageId: string | null;
  openActivity: (messageId: string) => void;
  closeActivity: () => void;
}

export const RightPanelContext = createContext<RightPanelState | null>(null);

export function useRightPanel(): RightPanelState {
  const ctx = useContext(RightPanelContext);
  if (!ctx) {
    // Defensive fallback: return a no-op instance so the components still
    // render outside the page (e.g. in a storybook or share-page preview).
    return {
      rightPanel: null,
      activityMessageId: null,
      openActivity: () => {},
      closeActivity: () => {},
    };
  }
  return ctx;
}
