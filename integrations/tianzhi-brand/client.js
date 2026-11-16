/** 天智五号 client branding. Copyright (c) yangshaopeng. */
window.__ModuleLoader__.load({
  id: '@yangshaopeng/tianzhi-brand',
  factory(require) {
    const { jsx } = require('react/jsx-runtime');
    const { useRef, useLayoutEffect } = require('react');
    const NS = 'tianzhi.brand';
    const BRAND = '天智五号';
    const STYLE_ID = 'tianzhi-brand-style';
    const CSS = `
      [data-tianzhi-sidebar-name] {
        font: 700 23px/1.25 "Microsoft YaHei", "PingFang SC", sans-serif;
        letter-spacing: 0; white-space: nowrap; color: inherit;
      }
      span:has(> [data-tianzhi-sidebar-empty]) { display: none !important; }
      [data-tianzhi-hero-title] {
        font: 700 34px/1.25 "Microsoft YaHei", "PingFang SC", sans-serif;
        letter-spacing: 0; white-space: nowrap; color: inherit;
      }
      span:has(> [data-tianzhi-hero-title]) {
        display: inline-flex !important; width: auto !important; height: auto !important;
        flex: 0 0 auto !important;
      }
      div:has(> span > [data-tianzhi-hero-title]) > span:not(:has(> [data-tianzhi-hero-title])) {
        display: none !important;
      }
    `;
    const SidebarMark = () => jsx('span', { 'data-tianzhi-sidebar-empty': '', 'aria-hidden': true });
    const face = '"Microsoft YaHei", "PingFang SC", sans-serif';
    const BrandText = ({ t, hero = false }) => jsx('span', {
      style: { display: 'inline-flex', alignItems: 'baseline', gap: hero ? '8px' : '4px', whiteSpace: 'nowrap', fontFamily: face, lineHeight: 1.25, letterSpacing: 0 },
      children: [
        jsx('span', { style: { fontFamily: face, fontSize: hero ? 'clamp(28px, 3vw, 36px)' : '26px', fontWeight: 700, letterSpacing: 0, color: 'inherit' }, children: t('name') }),
        jsx('span', { style: { fontFamily: face, fontSize: hero ? '18px' : '16px', fontWeight: 400, letterSpacing: 0, color: 'inherit' }, children: t('subtitle') })
      ]
    });
    const SidebarName = ({ t }) => jsx('span', { 'data-tianzhi-sidebar-name': '', style: { display: 'inline-flex', whiteSpace: 'nowrap' }, children: BrandText({ t }) });
    function HeroTitle({ t }) {
      const anchor = useRef(null);
      useLayoutEffect(() => {
        const node = anchor.current;
        if (!node) return;
        const saved = new Map();
        const chain = [];
        const seen = new Set();
        let current = node;
        for (let index = 0; index < 18; index++) {
          const root = current.getRootNode?.();
          current = current.parentElement ?? root?.host ?? null;
          if (!current || seen.has(current) || current === node.ownerDocument.body || current === node.ownerDocument.documentElement) break;
          seen.add(current); chain.push(current);
          if (current.hasAttribute?.('data-content-phase')) break;
        }
        const copy = new Set(['探索未至之境', '预览版', '探索未至之境预览版', 'IntotheUnknown', 'Preview', 'IntotheUnknownPreview']);
        const remember = element => { if (!saved.has(element)) saved.set(element, element.style.cssText); };
        const update = () => {
          for (let index = 0; index < chain.length; index++) {
            const parent = chain[index];
            const old = [...parent.querySelectorAll('span')].filter(element => !element.contains(node) && copy.has(element.textContent.replace(/\s+/g, '')));
            if (!old.length) continue;
            for (const element of old) { remember(element); element.style.setProperty('display', 'none', 'important'); }
            // The owning slot may add intermediate nodes. Expand only our mark
            // branch, leaving the original headline's layout and composer intact.
            for (const element of chain.slice(0, index)) {
              remember(element);
              for (const [key, value] of [['width','auto'],['height','auto'],['display','inline-flex'],['flex','0 0 auto']]) element.style.setProperty(key, value, 'important');
            }
            break;
          }
        };
        update();
        const observer = new MutationObserver(update);
        const boundary = chain.at(-1);
        if (boundary) observer.observe(boundary, {subtree: true, childList: true, characterData: true});
        return () => { observer.disconnect(); for (const [element, css] of saved) element.style.cssText = css; };
      }, []);
      return jsx('span', { ref: anchor, 'data-tianzhi-hero-title': '', style: {fontFamily: face, display: 'inline-flex', whiteSpace: 'nowrap'}, role: 'heading', 'aria-level': 1, children: BrandText({ t, hero: true }) });
    }
    const inject = ['slots', 'locale'];
    function apply(ctx) {
      ctx.effect(() => ctx.locale.register(NS, { zh: { name: BRAND, subtitle: '配电网潮流计算' }, en: { name: BRAND, subtitle: '配电网潮流计算' } }));
      ctx.effect(() => {
        if (document.getElementById(STYLE_ID)) return;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = CSS;
        document.head.append(style);
        return () => style.remove();
      });
      ctx.slots.inject('sidebar.brand.mark', () => ctx.slots.inject('sidebar.brand.name', function* () {
        yield ctx.slots.register({ name: 'sidebar.brand.mark', locale: NS }, SidebarMark);
        yield ctx.slots.register({ name: 'sidebar.brand.name', locale: NS }, SidebarName);
      }));
      ctx.slots.inject('conversation.hero.brand.mark', () => ctx.slots.register({ name: 'conversation.hero.brand.mark', locale: NS }, HeroTitle));
      ctx.effect(() => {
        const title = document.querySelector('title');
        const update = () => {
          const next = document.title.replace(/DeepSeek Harness$/, BRAND);
          if (document.title !== next) document.title = next;
        };
        update();
        if (!title) return;
        const observer = new MutationObserver(update);
        observer.observe(title, { subtree: true, childList: true, characterData: true });
        return () => { observer.disconnect(); document.title = document.title.replace(/天智五号$/, 'DeepSeek Harness'); };
      });
    }
    return { inject, apply };
  }
});
