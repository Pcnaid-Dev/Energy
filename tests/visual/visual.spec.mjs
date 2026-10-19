import { expect, test } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const visualDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(visualDir, '../..');
const stateConfig = JSON.parse(
  await readFile(path.join(repoRoot, 'ui-states.json'), 'utf8')
);
const pairArtifactPath = path.join(repoRoot, 'visual-out', 'pairs.json');

const RATE_WIDGET_FIXTURE = {
  zip: '76063',
  usage: '1,000 kWh',
  utilities: [
    { label: 'Oncor Electric Delivery', value: 'oncor' },
    { label: 'CenterPoint Energy', value: 'centerpoint' },
    { label: 'AEP Texas Central', value: 'aep-central' }
  ]
};

const WIDGET_FIXTURE_SCRIPT = String.raw`
(function () {
  function escapeHtml(value) {
    var node = document.createElement('span');
    node.textContent = String(value);
    return node.innerHTML;
  }

  fetch('https://api.myservicecloud.net/visual-fixture')
    .then(function (response) { return response.json(); })
    .then(function (fixture) {
      var mount = document.getElementById('widgetMount');
      var loading = document.getElementById('widgetLoading');
      if (!mount) return;
      if (loading) loading.remove();

      var options = fixture.utilities.map(function (utility, index) {
        return '<li class="el-select-dropdown__item' + (index === 0 ? ' selected' : '') +
          '" role="option" aria-selected="' + (index === 0 ? 'true' : 'false') + '">' +
          escapeHtml(utility.label) + '</li>';
      }).join('');

      var root = document.createElement('div');
      root.id = 'msc';
      root.className = 'visual-widget-fixture';
      root.setAttribute('data-fixture', 'rate-widget');
      root.innerHTML =
        '<style>' +
          '.visual-widget-fixture{background:#fff;border:1px solid #E5E7EB;border-radius:12px;padding:24px;font-family:var(--font-body,Arial,sans-serif);color:#111827}' +
          '.visual-widget-fixture h3{margin:0 0 6px;font-size:22px}' +
          '.visual-widget-fixture p{margin:0 0 20px;color:#64748B}' +
          '.visual-widget-fields{display:grid;grid-template-columns:1fr 1.6fr 1fr;gap:16px;align-items:end}' +
          '.visual-widget-field label{display:block;margin-bottom:7px;font-size:13px;font-weight:600;color:#334155}' +
          '.visual-widget-field input{width:100%;height:44px;border:1px solid #E5E7EB;border-radius:8px;padding:0 12px;background:#fff;color:#111827;font:inherit}' +
          '.visual-widget-fixture .el-select{width:100%;cursor:pointer}' +
          '.visual-widget-fixture .el-select:focus{outline:3px solid rgba(0,61,165,.25);outline-offset:2px}' +
          '.visual-widget-fixture .el-select-dropdown{width:100%;min-width:220px}' +
          '.visual-widget-fixture .el-select-dropdown__list{list-style:none;margin:0;padding:4px 0}' +
          '.visual-widget-fixture .fixture-submit{width:100%;height:44px;border:0;border-radius:8px;background:#087F5B;color:#fff;font:600 15px var(--font-body,Arial,sans-serif);cursor:pointer}' +
          '@media(max-width:640px){.visual-widget-fixture{padding:18px}.visual-widget-fields{grid-template-columns:1fr}.visual-widget-fixture .el-select-dropdown{min-width:0}}' +
        '</style>' +
        '<h3>Find plans for your service area</h3>' +
        '<p>Enter your location and usage to compare available electricity rates.</p>' +
        '<div class="visual-widget-fields">' +
          '<div class="visual-widget-field"><label for="fixtureZip">ZIP code</label><input id="fixtureZip" value="' + escapeHtml(fixture.zip) + '" readonly></div>' +
          '<div class="visual-widget-field"><label id="fixtureUtilityLabel">Utility</label>' +
            '<div class="el-select" role="combobox" tabindex="0" aria-labelledby="fixtureUtilityLabel" aria-expanded="false">' +
              '<div class="el-input"><input class="el-input__inner" value="' + escapeHtml(fixture.utilities[0].label) + '" readonly tabindex="-1"></div>' +
              '<div class="el-select-dropdown" style="display: none" role="listbox"><ul class="el-select-dropdown__list">' + options + '</ul></div>' +
            '</div>' +
          '</div>' +
          '<div class="visual-widget-field"><label>Monthly usage</label><button class="fixture-submit" type="button">Compare ' + escapeHtml(fixture.usage) + '</button></div>' +
        '</div>';
      mount.appendChild(root);

      var select = root.querySelector('.el-select');
      var dropdown = root.querySelector('.el-select-dropdown');
      function setOpen(open) {
        select.classList.toggle('is-open', open);
        select.setAttribute('aria-expanded', open ? 'true' : 'false');
        dropdown.style.display = open ? 'block' : 'none';
      }
      function isOutsideSelect(target) {
        return !select.contains(target);
      }
      select.addEventListener('click', function (event) {
        if (!event.target.closest('.el-select-dropdown')) {
          setOpen(true);
        }
      });
      document.addEventListener('click', function (event) {
        if (isOutsideSelect(event.target)) {
          setOpen(false);
        }
      });
      select.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          setOpen(!select.classList.contains('is-open'));
        }
      });
      window.__VISUAL_WIDGET_FIXTURE_READY__ = true;
    });
})();
`;

async function installNetworkFixtures(page) {
  await page.route('**/*', async (route) => {
    const requestUrl = new URL(route.request().url());
    const isWidgetApi = /^api(?:-[a-z0-9]+)?\.myservicecloud\.net$/.test(requestUrl.hostname);

    if (isWidgetApi) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { 'access-control-allow-origin': '*' },
        body: JSON.stringify(RATE_WIDGET_FIXTURE)
      });
      return;
    }

    if (
      requestUrl.hostname === 'myservicecloud.net' &&
      requestUrl.pathname === '/uploads/widgets/embed_comm_pricing.js'
    ) {
      await route.fulfill({
        status: 200,
        contentType: 'application/javascript',
        body: WIDGET_FIXTURE_SCRIPT
      });
      return;
    }

    if (requestUrl.hostname.endsWith('myservicecloud.net')) {
      await route.abort('blockedbyclient');
      return;
    }

    if (requestUrl.hostname === 'www.googletagmanager.com') {
      await route.abort('blockedbyclient');
      return;
    }

    await route.continue();
  });
}

async function performAction(page, action) {
  const locator = page.locator(action.selector);
  await locator.waitFor({ state: 'visible' });

  switch (action.action) {
    case 'click':
      await locator.click();
      break;
    case 'hover':
      await locator.hover();
      break;
    case 'focus': {
      let reached = false;
      for (let i = 0; i < 60; i++) {
        await page.keyboard.press('Tab');
        reached = await locator.evaluate((el) => el === document.activeElement);
        if (reached) break;
      }
      if (!reached) {
        throw new Error('keyboard focus never reached ' + action.selector);
      }
      expect(await locator.evaluate((el) => el.matches(':focus-visible'))).toBe(true);
      break;
    }
    case 'fill':
      await locator.fill(action.value ?? '');
      break;
    default:
      throw new Error(`Unsupported ui-states.json action: ${action.action}`);
  }

  if (action.waitFor) {
    const waitTarget = page.locator(action.waitFor);
    await waitTarget.waitFor({ state: 'visible' });
    await expect(waitTarget).toBeVisible();
  }
}

async function readPairControl(page, control) {
  const locator = page.locator(control.selector).first();
  if (!(await locator.isVisible())) {
    return {
      name: control.name,
      selector: control.selector,
      visible: false
    };
  }

  const values = await locator.evaluate((element) => {
    const style = getComputedStyle(element);
    const range = document.createRange();
    range.selectNodeContents(element);
    const labelBox = range.getBoundingClientRect();
    const buttonBox = element.getBoundingClientRect();
    const labelCentre = labelBox.left + labelBox.width / 2;
    const buttonCentre = buttonBox.left + buttonBox.width / 2;
    const offset = Math.abs(labelCentre - buttonCentre);
    const firstFontFamily = style.fontFamily
      .split(',')[0]
      .trim()
      .replace(/^(['"])(.*)\1$/, '$2');

    return {
      color: style.color,
      fontFamily: firstFontFamily,
      fontWeight: style.fontWeight,
      textAlign: style.textAlign,
      horizontalCentring: {
        centred: offset <= 2,
        offsetPx: Math.round(offset * 1000) / 1000
      }
    };
  });

  return {
    name: control.name,
    selector: control.selector,
    visible: true,
    values
  };
}

function comparedValue(control, property) {
  if (property === 'horizontal-centring') {
    const centring = control.values.horizontalCentring;
    return `${centring.centred ? 'centred' : 'not centred'} (${centring.offsetPx}px offset)`;
  }
  return control.values[property];
}

export async function compareDeclaredPairs(page, pairs) {
  const results = [];

  for (const pair of pairs) {
    if (!Array.isArray(pair.controls) || pair.controls.length !== 2) {
      throw new Error(`Pair "${pair.name}" must declare exactly two controls.`);
    }

    const controls = [];
    for (const control of pair.controls) {
      controls.push(await readPairControl(page, control));
    }

    const hiddenControls = controls.filter((control) => !control.visible);
    if (hiddenControls.length > 0) {
      results.push({
        name: pair.name,
        status: 'not-assessed',
        reason: `Selector not visible: ${hiddenControls.map((control) => control.selector).join(', ')}`,
        controls,
        checks: []
      });
      continue;
    }

    const properties = [
      ['color', 'color'],
      ['font-family', 'fontFamily'],
      ['font-weight', 'fontWeight'],
      ['text-align', 'textAlign'],
      ['horizontal-centring', 'horizontal-centring']
    ];
    const checks = properties.map(([property, valueKey]) => {
      const firstValue = comparedValue(controls[0], valueKey);
      const secondValue = comparedValue(controls[1], valueKey);
      const matches = property === 'horizontal-centring'
        ? controls[0].values.horizontalCentring.centred === controls[1].values.horizontalCentring.centred
        : firstValue === secondValue;
      return {
        property,
        matches,
        values: [
          { control: controls[0].name, value: firstValue },
          { control: controls[1].name, value: secondValue }
        ]
      };
    });

    results.push({
      name: pair.name,
      status: checks.every((check) => check.matches) ? 'pass' : 'fail',
      controls,
      checks
    });
  }

  return results;
}

async function recordPairResults(route, viewport, state, image, pairs) {
  const result = {
    image,
    route: route.path,
    viewport,
    state: state.name,
    pairs
  };
  await mkdir(path.dirname(pairArtifactPath), { recursive: true });
  let artifact = { results: [] };
  try {
    artifact = JSON.parse(await readFile(pairArtifactPath, 'utf8'));
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  artifact.results = artifact.results.filter((entry) => entry.image !== image);
  artifact.results.push(result);
  await writeFile(
    pairArtifactPath,
    `${JSON.stringify(artifact, null, 2)}\n`
  );
}

function expectMatchingPairs(pairResults) {
  for (const pair of pairResults) {
    for (const check of pair.checks) {
      const [first, second] = check.values;
      expect.soft(
        check.matches,
        `Pair "${pair.name}" ${check.property} mismatch: ` +
          `${first.control}=${JSON.stringify(first.value)}, ` +
          `${second.control}=${JSON.stringify(second.value)}`
      ).toBe(true);
    }
  }
}

test.beforeAll(async ({}, testInfo) => {
  if (testInfo.workerIndex !== 0) return;
  await mkdir(path.dirname(pairArtifactPath), { recursive: true });
  await writeFile(pairArtifactPath, `${JSON.stringify({ results: [] }, null, 2)}\n`);
});

for (const [viewportName, viewport] of Object.entries(stateConfig.viewports)) {
  test.describe(viewportName, () => {
    test.use({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: viewport.deviceScaleFactor,
      isMobile: viewport.isMobile,
      hasTouch: viewport.hasTouch
    });

    for (const route of stateConfig.routes) {
      for (const state of route.states[viewportName]) {
        test(`${route.id} / ${state.name}`, async ({ page }) => {
          await installNetworkFixtures(page);
          await page.goto(route.path, { waitUntil: 'load' });

          if (route.path === '/compare.html') {
            await page.locator('#widgetMount .el-select').waitFor({ state: 'visible' });
          }

          await page.evaluate(async () => {
            if (document.fonts) await document.fonts.ready;
          });

          if (state.scrollIntoView) {
            const scrollTarget = page.locator(state.scrollIntoView);
            await scrollTarget.waitFor({ state: 'visible' });
            await scrollTarget.evaluate((element) => {
              element.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
            });
          }

          for (const action of state.actions) {
            await performAction(page, action);
          }

          await page.evaluate(() => new Promise((resolve) => {
            requestAnimationFrame(() => requestAnimationFrame(resolve));
          }));

          const screenshotName = `${route.id}-${viewportName}-${state.name}.png`;
          if (state.pairs.length > 0) {
            const pairResults = await compareDeclaredPairs(page, state.pairs);
            await recordPairResults(
              route,
              viewportName,
              state,
              screenshotName,
              pairResults
            );
            expectMatchingPairs(pairResults);
          }

          let clip;
          if (state.clip) {
            const clipTarget = page.locator(state.clip);
            await clipTarget.scrollIntoViewIfNeeded();
            clip = await clipTarget.boundingBox();
            if (!clip) throw new Error(`Clip selector is not visible: ${state.clip}`);
          }

          const outputPath = path.join(repoRoot, 'visual-out', screenshotName);
          const mask = stateConfig.mask.map((selector) => page.locator(selector));
          await mkdir(path.dirname(outputPath), { recursive: true });

          const image = await page.screenshot({
            animations: 'disabled',
            caret: 'hide',
            clip,
            fullPage: false,
            mask,
            scale: 'device'
          });
          await writeFile(outputPath, image);

          await expect(page).toHaveScreenshot(screenshotName, {
            animations: 'disabled',
            caret: 'hide',
            clip,
            fullPage: false,
            mask,
            maxDiffPixelRatio: 0.001,
            scale: 'device'
          });
        });
      }
    }
  });
}

test.describe('deterministic pair comparison regression', () => {
  const viewport = stateConfig.viewports.mobile;
  test.use({
    viewport: { width: viewport.width, height: viewport.height },
    deviceScaleFactor: viewport.deviceScaleFactor,
    isMobile: viewport.isMobile,
    hasTouch: viewport.hasTouch
  });

  test('rejects a serif drawer CTA override (d317 e)', async ({ page }) => {
    const route = stateConfig.routes.find((candidate) => candidate.id === 'root');
    const state = route.states.mobile.find((candidate) => candidate.name === 'drawer-open');

    await installNetworkFixtures(page);
    await page.goto(route.path, { waitUntil: 'load' });
    await page.evaluate(async () => {
      if (document.fonts) await document.fonts.ready;
    });
    for (const action of state.actions) {
      await performAction(page, action);
    }
    await page.addStyleTag({
      content: '.mobile-menu .btn-primary { font-family: serif !important; }'
    });

    const [result] = await compareDeclaredPairs(page, state.pairs);
    const fontFamily = result.checks.find((check) => check.property === 'font-family');

    expect(result.status).toBe('fail');
    expect(fontFamily.matches).toBe(false);
  });
});
