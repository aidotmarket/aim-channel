import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CoPilotFab from "@/components/copilot/CoPilotFab";
import { CoPilotProvider, useCoPilot } from "./CoPilotContext";

vi.mock("./AuthContext", () => ({
  useAuth: () => ({ apiKey: "test-token", isAuthenticated: true }),
}));

vi.mock("./ModeContext", () => ({
  useMode: () => ({
    channel: "aim-data",
    hasFeature: (name: string) => name === "allai",
    isLoading: false,
  }),
}));

vi.mock("@/lib/api", () => ({
  datasetsApi: {
    list: vi.fn().mockResolvedValue({ datasets: [] }),
  },
}));

vi.mock("@/api/copilotApi", () => ({
  copilotApi: {
    currentMessages: vi.fn().mockResolvedValue([]),
    websocketUrl: vi.fn().mockReturnValue("ws://example.test/ws/copilot"),
  },
}));

class MockWebSocket {
  static OPEN = 1;
  static instances: MockWebSocket[] = [];

  readyState = MockWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();

  constructor() {
    MockWebSocket.instances.push(this);
  }
}

function StateProbe() {
  const { isOpen, hasUnread, messages } = useCoPilot();

  return (
    <>
      <div data-testid="is-open">{String(isOpen)}</div>
      <div data-testid="has-unread">{String(hasUnread)}</div>
      <div data-testid="message-count">{messages.length}</div>
      <div data-testid="last-message">{messages.at(-1)?.content ?? ""}</div>
      <CoPilotFab />
    </>
  );
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal("localStorage", {
    getItem: vi.fn().mockReturnValue(null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
  });
  vi.stubGlobal("WebSocket", MockWebSocket);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("CoPilotProvider unread messages", () => {
  it("queues the CONNECTED welcome without opening and clears unread when opened", async () => {
    render(
      <MemoryRouter>
        <CoPilotProvider>
          <StateProbe />
        </CoPilotProvider>
      </MemoryRouter>
    );

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => {
      MockWebSocket.instances[0].onmessage?.({
        data: JSON.stringify({
          type: "CONNECTED",
          session_id: "session-1",
          allie_available: true,
        }),
      } as MessageEvent);
    });

    expect(screen.getByTestId("message-count")).toHaveTextContent("1");
    expect(screen.getByTestId("last-message")).toHaveTextContent(
      "Hi, I'm allAI (pronounced \"Ally\"), your AIM Data assistant."
    );
    expect(screen.getByTestId("is-open")).toHaveTextContent("false");
    expect(screen.getByTestId("has-unread")).toHaveTextContent("true");
    expect(screen.getByLabelText("Unread allAI messages")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("Open allAI"));

    expect(screen.getByTestId("is-open")).toHaveTextContent("true");
    expect(screen.getByTestId("has-unread")).toHaveTextContent("false");
  });
});
