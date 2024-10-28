(function () {
    function setAttributes(scriptEl) {
        const d = window.document;
        const initScript = d.getElementById('app');
        const {
            apiKey, authKey, demoMode, showGreetings, identifier, widgetType, embeddedToMsc, quoteUid,
            config,
        } = {...initScript.dataset};
        scriptEl.setAttribute('id', 'app');
        scriptEl.setAttribute('data-api-key', apiKey);
        if (authKey) {
            scriptEl.setAttribute('data-auth-key', authKey);
        }
        if (demoMode) {
            scriptEl.setAttribute('data-demo-mode', demoMode);
        }
        if (showGreetings) {
            scriptEl.setAttribute('data-show-greetings', showGreetings);
        }
        if (identifier && identifier !== undefined) {
            scriptEl.setAttribute('data-identifier', identifier);
        }
        if (widgetType) {
            scriptEl.setAttribute('data-widget-type', widgetType);
        }
        if (embeddedToMsc !== undefined) {
            scriptEl.setAttribute('data-embedded-to-msc', embeddedToMsc);
        }
        if (quoteUid !== undefined) {
            scriptEl.setAttribute('data-quote-uid', quoteUid);
        }
        if (config !== undefined) {
            scriptEl.setAttribute('data-config', config);
        }

        return scriptEl;
    }

    const d = window.document;
    const initScript = d.getElementById('app');
    const preloaderScript = setAttributes(d.createElement('script'));
    preloaderScript.src = 'embed_comm_pricing_loader.js?v' + Math.random();
    initScript.parentNode.replaceChild(preloaderScript, initScript);
})();