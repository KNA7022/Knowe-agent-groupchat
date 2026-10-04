// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useSettingsStore } from '../store/settings';
import FirstRunModelGate from './FirstRunModelGate';

vi.mock('./ModelBindingModule', () => ({ default: () => <div>模型配置</div> }));
vi.mock('./PrimaryLanguageModule', () => ({ default: () => null }));
vi.mock('react-i18next', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-i18next')>(),
  useTranslation: () => ({ t: (key: string) => key }),
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('opens the local-use path with no key/network without asserting model readiness', () => {
  useSettingsStore.setState({ mainModel: null, modelApplyState: 'idle', appliedFingerprint: null, zinniaCompatible: false });
  const navigateLocal = vi.fn();
  const fetch = vi.fn();
  vi.stubGlobal('fetch', fetch);
  render(<FirstRunModelGate onContinueLocal={navigateLocal} />);
  expect(screen.getByRole('button', { name: 'first.run.model.gate.enter' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'first.run.model.gate.continueLocal' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(navigateLocal).toHaveBeenCalledOnce();
  expect(fetch).not.toHaveBeenCalled();
  expect(useSettingsStore.getState()).toMatchObject({ mainModel: null, modelApplyState: 'idle', appliedFingerprint: null, zinniaCompatible: false });
});
