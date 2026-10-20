import test from 'node:test';
import assert from 'node:assert/strict';
import {THEMES, DEFAULT_THEME, resolveTheme} from '../src/themes.js';
import {routeInfo, mediaURL} from '../src/api.js';

test('four complete themes and invalid persisted values fall back safely', () => {
  assert.deepEqual(THEMES.map(theme=>theme.id), ['midnight','glass','acid','editorial']);
  for(const theme of THEMES) {
    assert.equal(resolveTheme(theme.id),theme.id);
    assert.ok(theme.name && theme.description && theme.subtitle);
    assert.equal(theme.swatches.length,4);
  }
  for(const value of [null,undefined,'','__proto__','<script>','neon']) assert.equal(resolveTheme(value),DEFAULT_THEME);
});

test('hash routes keep project identity, tabs, and filter query independent', () => {
  assert.equal(routeInfo('').key,'dashboard');
  const route=routeInfo('#/projects/project%20one/chat?status=running');
  assert.equal(route.key,'projects');
  assert.deepEqual(route.params,['project one','chat']);
  assert.equal(route.query.get('status'),'running');
  assert.equal(routeInfo('#/settings/advanced').params[0],'advanced');
});

test('native project previews cannot escape the local media route', () => {
  assert.equal(mediaURL('projects/one/my clip.mp4'),'/media/projects/one/my%20clip.mp4');
  for(const path of ['', '../secret','/absolute','x/../../.env','C:\\secret','https://elsewhere/video.mp4']) assert.equal(mediaURL(path),'');
});
