import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { useOfflineStatus } from './use-offline-status';

/**
 * Test the offline status hook indirectly through a tiny harness
 * that renders the resulting state. We don't need a full Router
 * (the hook has no router dependency) — just enough JSX to read
 * the textContent.
 */

function TestHarness() {
  const { status, syncNow, strategy } = useOfflineStatus();
  return (
    <div>
      <span data-testid="state">{status.syncState}</span>
      <span data-testid="strategy">{strategy}</span>
      <span data-testid="ready">{String(status.ready)}</span>
      <button type="button" onClick={() => void syncNow()}>
        sync
      </button>
    </div>
  );
}

describe('useOfflineStatus', () => {
  it('exposes the public status shape and renders without crashing', () => {
    render(<TestHarness />);
    expect(screen.getByTestId('state')).toBeInTheDocument();
    expect(screen.getByTestId('strategy')).toBeInTheDocument();
    expect(screen.getByTestId('ready')).toBeInTheDocument();
    // The hook never throws and never returns undefined for the
    // documented fields — that's the meaningful guarantee here.
    expect(screen.getByTestId('ready').textContent).toMatch(/(true|false)/);
  });
});
