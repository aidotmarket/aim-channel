import { MessageCircle } from "lucide-react";
import { useCoPilot } from "@/contexts/CoPilotContext";

export default function CoPilotFab() {
  const { toggle, isOpen, hasUnread, allieAvailable, embeddedSurfaceActive } = useCoPilot();

  if (!allieAvailable) return null;
  if (embeddedSurfaceActive) return null;
  if (isOpen) return null;

  return (
    <button
      onClick={toggle}
      className="fixed bottom-5 right-5 z-40 h-12 w-12 rounded-full flex items-center justify-center shadow-lg transition-all hover:scale-105 active:scale-95 border border-border"
      style={{
        background: "rgba(255, 255, 255, 0.9)",
        backdropFilter: "blur(16px)",
        WebkitBackdropFilter: "blur(16px)",
      }}
      title="Open allAI"
    >
      <MessageCircle className="h-5 w-5 text-primary/70" />
      {hasUnread && (
        <span
          aria-label="Unread allAI messages"
          className="absolute right-1 top-1 h-2.5 w-2.5 rounded-full bg-primary"
        />
      )}
    </button>
  );
}
