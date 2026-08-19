import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';

let openModalCount = 0;
let previousAriaHidden: string | null = null;
let previousInert = false;

function overlayHost(): HTMLElement {
  const existing = document.getElementById('studycrew-overlay-host');
  if (existing) return existing;
  const host = document.createElement('div');
  host.id = 'studycrew-overlay-host';
  host.className = 'study-overlay-host';
  document.body.append(host);
  return host;
}

function setBackgroundIsolated(isolated: boolean): void {
  const root = document.getElementById('root');
  if (!root) return;
  const inertRoot = root as HTMLElement & { inert: boolean };
  if (isolated) {
    previousAriaHidden = root.getAttribute('aria-hidden');
    previousInert = Boolean(inertRoot.inert);
    inertRoot.inert = true;
    root.setAttribute('aria-hidden', 'true');
    return;
  }
  inertRoot.inert = previousInert;
  if (previousAriaHidden === null) root.removeAttribute('aria-hidden');
  else root.setAttribute('aria-hidden', previousAriaHidden);
}

export const ModalPortal: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [host] = useState(overlayHost);

  useEffect(() => {
    openModalCount += 1;
    if (openModalCount === 1) setBackgroundIsolated(true);
    return () => {
      openModalCount = Math.max(0, openModalCount - 1);
      if (openModalCount === 0) setBackgroundIsolated(false);
    };
  }, []);

  return createPortal(children, host);
};

export default ModalPortal;
