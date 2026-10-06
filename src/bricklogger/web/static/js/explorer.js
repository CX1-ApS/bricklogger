/* The model explorer.
 *
 * Draws the entity document of GET /v1/entities: a hierarchy on the left, the
 * selected element's neighbourhood in the middle, its details on the right.
 * The hierarchy rules are the ones in docs/features/web.md, "Model explorer",
 * and they are implemented the same way on the server in daemon/explorer.py,
 * so both interfaces draw one building out of one model.
 */
(function () {
  'use strict';

  var CONFIG = window.EXPLORER || {};
  var T = CONFIG.texts || {};
  var KINDS = ['location', 'equipment', 'point', 'system', 'other'];
  var GLYPHS = { location: '\u25a1', equipment: '\u25a2', point: '\u25cb', system: '\u25c7', other: '\u00b7' };
  var SHAPES = { location: 'rectangle', equipment: 'round-rectangle', point: 'ellipse', system: 'diamond', other: 'rectangle' };
  var CONTAINER_ORDER = {
    point: ['point', 'location'],
    equipment: ['location', 'part'],
    location: ['part', 'location'],
    system: ['part', 'location'],
    other: ['part', 'location']
  };
  var ROOT_KINDS = ['location', 'system'];
  var GRAPH_CAP = 300;
  var WHEEL_SENSITIVITY = 1;   // Cytoscape's own default; less crawls
  var PANE_WIDTHS = 'bricklogger.explorer.panes';
  var MIN_PANE = 220;
  var EXPAND_CAP = 5000;
  var FILTER_KEYS = ['q', 'kind', 'class', 'finding', 'outcome', 'warning', 'instance', 'sort', 'depth'];

  var model = null;        // the document as delivered
  var index = null;        // uri -> entity, adjacency, parents, children
  var state = {};          // the filters, mirrored in the address
  var visible = null;      // uri -> true for everything the filters keep
  var matched = null;      // uri -> true for what matches, context excluded
  var expanded = {};       // uri -> true
  var selected = null;
  var cy = null;
  var collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

  function t(key, values) {
    var text = T[key] || key;
    if (!values) return text;
    return text.replace(/\{(\w+)\}/g, function (whole, name) {
      return Object.prototype.hasOwnProperty.call(values, name) ? values[name] : whole;
    });
  }

  function el(id) { return document.getElementById(id); }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments, self = this;
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { fn.apply(self, args); }, wait);
    };
  }

  // --- loading -------------------------------------------------------------

  function boot() {
    readState();
    bindControls();
    bindDividers();
    document.body.addEventListener('htmx:afterSwap', onStatusSwap);
    loadModel();
  }

  function loadModel() {
    var body = el('tree-body');
    body.replaceChildren(make('p', 'terminal', t('loading')));
    fetch(CONFIG.endpoint, { headers: { Accept: 'application/json' } })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            throw new Error(data.detail || data.title || response.status);
          }
          return data;
        });
      })
      .then(function (data) {
        model = data;
        CONFIG.modelVersion = data.version;
        var label = el('model-version');
        if (label) label.textContent = t('version', { version: data.version });
        index = indexDocument(data);
        buildCatalogues();
        openTheBuilding();
        applyFilters();
        var hash = decodeURIComponent((window.location.hash || '').slice(1));
        if (hash && index.byUri[hash]) select(hash, true);
        clearNotice();
      })
      .catch(function (error) {
        body.replaceChildren(make('p', 'notice signal', t('failed', { detail: error.message })));
      });
  }

  function indexDocument(document_) {
    var byUri = {};
    document_.entities.forEach(function (entity) { byUri[entity.uri] = entity; });
    var out = {}, into = {}, candidates = {}, gathered = {};
    document_.relations.forEach(function (relation) {
      if (!byUri[relation.subject] || !byUri[relation.object]) return;
      (out[relation.subject] = out[relation.subject] || []).push(relation);
      (into[relation.object] = into[relation.object] || []).push(relation);
      if (!relation.role || !relation.child || relation.subject === relation.object) return;
      var lower = relation.child === 'subject' ? relation.subject : relation.object;
      var upper = relation.child === 'subject' ? relation.object : relation.subject;
      // A grouping gathers what is placed elsewhere: its line counts them,
      // the hierarchy leaves it out at either end. See the rules in web.md.
      if (byUri[upper].grouping) {
        (gathered[upper] = gathered[upper] || {})[lower] = true;
        return;
      }
      if (byUri[lower].grouping) return;
      var found = candidates[lower] = candidates[lower] || {};
      (found[relation.role] = found[relation.role] || []).push(upper);
    });

    // One container per element, by kind: see the rules in web.md.
    var parents = {};
    Object.keys(candidates).forEach(function (uri) {
      var order = CONTAINER_ORDER[byUri[uri].kind] || ['part', 'location'];
      for (var i = 0; i < order.length; i++) {
        var found = candidates[uri][order[i]];
        if (found && found.length) {
          parents[uri] = found.slice().sort()[0];
          return;
        }
      }
    });
    breakCycles(parents);

    var children = {}, roots = [], groupings = [], unplaced = [];
    Object.keys(byUri).sort().forEach(function (uri) {
      var parent = parents[uri];
      if (parent) (children[parent] = children[parent] || []).push(uri);
      else if (byUri[uri].grouping) groupings.push(uri);
      else if (ROOT_KINDS.indexOf(byUri[uri].kind) >= 0) roots.push(uri);
      else unplaced.push(uri);
    });
    var points = {};
    function countPoints(uri) {
      if (points[uri] !== undefined) return points[uri];
      points[uri] = 0;  // guards a malformed document
      var total = byUri[uri].kind === 'point' ? 1 : 0;
      (children[uri] || []).forEach(function (child) { total += countPoints(child); });
      points[uri] = total;
      return total;
    }
    Object.keys(byUri).forEach(countPoints);
    roots.sort(function (a, b) {
      var kinds = ROOT_KINDS.indexOf(byUri[a].kind) - ROOT_KINDS.indexOf(byUri[b].kind);
      return kinds !== 0 ? kinds : collator.compare(a, b);
    });
    return {
      byUri: byUri, out: out, into: into, parents: parents,
      children: children, roots: roots, unplaced: unplaced, points: points,
      gathered: gathered, band: buildBand(byUri, groupings)
    };
  }

  // The groupings under a heading for the family that makes each one a
  // grouping and, beneath it, one for its own class: two deep in every model,
  // so that a whole family folds away with one click.
  function buildBand(byUri, groupings) {
    var families = {};
    groupings.forEach(function (uri) {
      var family = byUri[uri].grouping;
      var name = byUri[uri].class || family;
      var classes = families[family] = families[family] || {};
      (classes[name] = classes[name] || []).push(uri);
    });
    return Object.keys(families).sort(function (a, b) {
      return collator.compare(a, b);
    }).map(function (family) {
      return { family: family, classes: families[family] };
    });
  }

  function membersOf(uri) {
    return Object.keys(index.gathered[uri] || {}).length;
  }

  function breakCycles(parents) {
    Object.keys(parents).sort().forEach(function (start) {
      var seen = [start], walker = parents[start];
      while (walker) {
        var at = seen.indexOf(walker);
        if (at >= 0) {
          delete parents[seen.slice(at).sort()[0]];
          return;
        }
        seen.push(walker);
        walker = parents[walker];
      }
    });
  }

  // --- the filter bar ------------------------------------------------------

  function buildCatalogues() {
    var kinds = {}, classes = {}, findings = {}, outcomes = {}, warnings = {}, instances = {};
    model.entities.forEach(function (entity) {
      kinds[entity.kind] = (kinds[entity.kind] || 0) + 1;
      (entity.types || []).forEach(function (type) { classes[type] = (classes[type] || 0) + 1; });
      (entity.findings || []).forEach(function (code) { findings[code] = (findings[code] || 0) + 1; });
      (entity.warnings || []).forEach(function (code) { warnings[code] = (warnings[code] || 0) + 1; });
      if (entity.runtime) {
        if (entity.runtime.outcome) outcomes[entity.runtime.outcome] = true;
        if (entity.runtime.instance) instances[entity.runtime.instance] = true;
      }
    });

    var box = el('kinds');
    box.replaceChildren(make('legend', null, t('filter.kind')));
    KINDS.forEach(function (kind) {
      if (!kinds[kind]) return;
      var label = make('label');
      var input = make('input');
      input.type = 'checkbox';
      input.value = kind;
      input.checked = state.kind ? state.kind.split(',').indexOf(kind) >= 0 : false;
      input.addEventListener('change', onFilterChange);
      label.append(input, document.createTextNode(t('kind.' + kind) + ' ' + kinds[kind]));
      box.append(label);
    });

    // Classes carry their ancestors, so a parent class also matches what
    // inherits from it — the subclass semantics the API's ?class= uses.
    fill('class', Object.keys(classes).sort(), function (name) { return name + ' (' + classes[name] + ')'; });
    fill('finding', Object.keys(findings).sort(), function (code) { return code + ' (' + findings[code] + ')'; });
    fill('outcome', Object.keys(outcomes).sort(), null);
    fill('warning', Object.keys(warnings).sort(), function (code) { return code + ' (' + warnings[code] + ')'; });
    fill('instance', Object.keys(instances).sort(), null);
    el('sort').value = state.sort || 'name';
    el('search').value = state.q || '';
    el('depth').value = state.depth || '1';
    if (state.class || state.finding || state.outcome || state.warning || state.instance) {
      el('more-filters').open = true;
    }
  }

  function fill(id, values, label) {
    var select = el(id);
    select.replaceChildren();
    var any = make('option', null, t('filter.any'));
    any.value = '';
    select.append(any);
    values.forEach(function (value) {
      var option = make('option', null, label ? label(value) : value);
      option.value = value;
      select.append(option);
    });
    select.value = state[id] || '';
    select.addEventListener('change', onFilterChange);
  }

  function bindControls() {
    el('search').addEventListener('input', debounce(onFilterChange, 150));
    el('sort').addEventListener('change', onFilterChange);
    el('clear').addEventListener('click', function () {
      state = {};
      buildCatalogues();
      writeState();
      applyFilters();
    });
    el('expand-all').addEventListener('click', expandAll);
    el('collapse-all').addEventListener('click', function () {
      expanded = {};
      renderTree();
    });
    el('depth').addEventListener('change', function () {
      state.depth = el('depth').value;
      writeState();
      renderGraph();
    });
    el('fit').addEventListener('click', function () { if (cy) cy.fit(undefined, 24); });
    el('png').addEventListener('click', exportPng);
    var reload = el('reload');
    if (reload) reload.addEventListener('click', loadModel);
  }

  // --- the dividers --------------------------------------------------------

  // The three panes are grid columns with a divider between them. A drag moves
  // width from one neighbour to the other and leaves the rest of the row alone,
  // so the layout cannot drift as the panes are pulled about.
  function bindDividers() {
    var grid = el('explorer');
    if (!grid) return;
    restoreWidths(grid);
    [el('divider-1'), el('divider-2')].forEach(function (divider, index) {
      if (!divider) return;
      divider.addEventListener('pointerdown', function (event) {
        if (window.innerWidth <= 1100) return;
        event.preventDefault();
        divider.setPointerCapture(event.pointerId);
        grid.classList.add('resizing');
        var panes = paneWidths(grid);
        var start = event.clientX;
        var before = panes[index];
        var after = panes[index + 1];

        var move = function (moved) {
          var by = moved.clientX - start;
          by = Math.max(MIN_PANE - before, Math.min(after - MIN_PANE, by));
          panes[index] = before + by;
          panes[index + 1] = after - by;
          applyWidths(grid, panes);
        };
        var up = function () {
          divider.releasePointerCapture(event.pointerId);
          grid.classList.remove('resizing');
          divider.removeEventListener('pointermove', move);
          divider.removeEventListener('pointerup', up);
          rememberWidths(grid);
          if (cy) cy.resize();
        };
        divider.addEventListener('pointermove', move);
        divider.addEventListener('pointerup', up);
      });
      divider.addEventListener('keydown', function (event) {
        var step = event.key === 'ArrowLeft' ? -24 : event.key === 'ArrowRight' ? 24 : 0;
        if (!step || window.innerWidth <= 1100) return;
        event.preventDefault();
        var panes = paneWidths(grid);
        step = Math.max(MIN_PANE - panes[index], Math.min(panes[index + 1] - MIN_PANE, step));
        panes[index] += step;
        panes[index + 1] -= step;
        applyWidths(grid, panes);
        rememberWidths(grid);
        if (cy) cy.resize();
      });
    });
    window.addEventListener('resize', function () { if (cy) cy.resize(); });
  }

  function paneWidths(grid) {
    return ['pane-tree', 'pane-graph', 'pane-details'].map(function (id) {
      return el(id).getBoundingClientRect().width;
    });
  }

  function applyWidths(grid, panes) {
    grid.style.gridTemplateColumns =
      panes[0] + 'px 14px ' + panes[1] + 'px 14px ' + panes[2] + 'px';
  }

  function rememberWidths(grid) {
    try {
      window.localStorage.setItem(PANE_WIDTHS, JSON.stringify(paneWidths(grid)));
    } catch (error) {
      /* a browser that refuses storage simply forgets the widths */
    }
  }

  function restoreWidths(grid) {
    if (window.innerWidth <= 1100) return;
    var saved = null;
    try {
      saved = JSON.parse(window.localStorage.getItem(PANE_WIDTHS) || 'null');
    } catch (error) {
      saved = null;
    }
    if (!Array.isArray(saved) || saved.length !== 3) return;
    if (!saved.every(function (n) { return typeof n === 'number' && n >= MIN_PANE; })) return;
    // The window may be a different size than when the widths were kept.
    var room = grid.getBoundingClientRect().width - 28;
    var total = saved[0] + saved[1] + saved[2];
    if (!room || !total) return;
    applyWidths(grid, saved.map(function (n) { return Math.round((n / total) * room); }));
  }

  function onFilterChange() {
    state.q = el('search').value.trim();
    state.sort = el('sort').value;
    var kinds = [];
    el('kinds').querySelectorAll('input:checked').forEach(function (input) { kinds.push(input.value); });
    state.kind = kinds.join(',');
    ['class', 'finding', 'outcome', 'warning', 'instance'].forEach(function (key) {
      state[key] = el(key).value;
    });
    writeState();
    applyFilters();
  }

  function readState() {
    var params = new URLSearchParams(window.location.search);
    FILTER_KEYS.forEach(function (key) {
      var value = params.get(key);
      if (value) state[key] = value;
    });
  }

  function writeState() {
    var params = new URLSearchParams();
    FILTER_KEYS.forEach(function (key) {
      if (state[key]) params.set(key, state[key]);
    });
    var query = params.toString();
    window.history.replaceState(null, '', window.location.pathname + (query ? '?' + query : '') + (window.location.hash || ''));
  }

  // --- filtering -----------------------------------------------------------

  function matches(entity) {
    if (state.kind) {
      if (state.kind.split(',').indexOf(entity.kind) < 0) return false;
    }
    if (state.class) {
      var types = entity.types || [];
      var hit = types.indexOf(state.class) >= 0;
      if (!hit) {
        hit = types.some(function (type) {
          var ancestors = (model.classes || {})[type] || [];
          return ancestors.indexOf(state.class) >= 0;
        });
      }
      if (!hit) return false;
    }
    if (state.finding && (entity.findings || []).indexOf(state.finding) < 0) return false;
    if (state.warning && (entity.warnings || []).indexOf(state.warning) < 0) return false;
    if (state.outcome) {
      if (!entity.runtime || entity.runtime.outcome !== state.outcome) return false;
    }
    if (state.instance) {
      if (!entity.runtime || entity.runtime.instance !== state.instance) return false;
    }
    if (state.q) {
      var needle = state.q.toLowerCase();
      if ((entity.uri + ' ' + (entity.name || '')).toLowerCase().indexOf(needle) < 0) return false;
    }
    return true;
  }

  function applyFilters() {
    matched = {};
    visible = {};
    var narrowed = FILTER_KEYS.some(function (key) {
      return key !== 'sort' && key !== 'depth' && state[key];
    });
    model.entities.forEach(function (entity) {
      if (!narrowed || matches(entity)) matched[entity.uri] = true;
    });
    Object.keys(matched).forEach(function (uri) {
      visible[uri] = true;
      var walker = index.parents[uri];
      while (walker && !visible[walker]) {
        visible[walker] = true;
        walker = index.parents[walker];
      }
    });
    // A filtered view is only useful open: show the path to every match.
    if (narrowed) {
      Object.keys(matched).forEach(function (uri) {
        var walker = index.parents[uri];
        while (walker) { expanded[walker] = true; walker = index.parents[walker]; }
      });
      expanded.__unplaced__ = true;
    }
    renderTree();
    renderCounts();
  }

  function renderCounts() {
    var entities = 0, findings = 0;
    model.entities.forEach(function (entity) {
      if (!matched[entity.uri]) return;
      entities += 1;
      if ((entity.findings || []).length) findings += 1;
    });
    el('counts').textContent = t('counts', {
      entities: entities, relations: model.relations.length, findings: findings
    });
  }

  // --- the tree ------------------------------------------------------------

  function sortedChildren(uris) {
    var sort = state.sort || 'name';
    return uris.slice().filter(function (uri) { return visible[uri]; }).sort(function (a, b) {
      var left = index.byUri[a], right = index.byUri[b];
      if (sort === 'count') {
        if (index.points[b] !== index.points[a]) return index.points[b] - index.points[a];
      } else if (sort === 'class') {
        var byClass = collator.compare(left.class || '', right.class || '');
        if (byClass !== 0) return byClass;
      }
      var byName = collator.compare(left.name || '', right.name || '');
      return byName !== 0 ? byName : collator.compare(a, b);
    });
  }

  function renderTree() {
    var body = el('tree-body');
    var roots = index.roots.filter(function (uri) { return visible[uri]; });
    var unplaced = index.unplaced.filter(function (uri) { return visible[uri]; });
    var band = visibleBand();
    if (!roots.length && !unplaced.length && !band.length) {
      body.replaceChildren(make('p', 'empty', t('empty')));
      return;
    }
    var tree = make('ul', 'tree');
    tree.setAttribute('role', 'tree');
    tree.addEventListener('keydown', onTreeKey);
    sortedChildren(roots).forEach(function (uri) { tree.append(renderItem(uri, 1)); });
    band.forEach(function (entry) { tree.append(renderFamily(entry)); });
    if (unplaced.length) tree.append(renderUnplaced(unplaced));
    body.replaceChildren(tree);
    if (!body.querySelector('.row[tabindex="0"]')) {
      var first = body.querySelector('.row');
      if (first) first.setAttribute('tabindex', '0');
    }
  }

  function visibleBand() {
    var band = [];
    index.band.forEach(function (entry) {
      var classes = [];
      Object.keys(entry.classes).sort(function (a, b) {
        return collator.compare(a, b);
      }).forEach(function (name) {
        var uris = entry.classes[name].filter(function (uri) { return visible[uri]; });
        if (uris.length) classes.push({ name: name, uris: uris });
      });
      if (classes.length) band.push({ family: entry.family, classes: classes });
    });
    return band;
  }

  function renderFamily(entry) {
    var key = familyKey(entry.family);
    var total = entry.classes.reduce(function (n, c) { return n + c.uris.length; }, 0);
    var item = renderHeading(key, entry.family, total, 1);
    if (!expanded[key]) return item;
    var list = make('ul');
    list.setAttribute('role', 'group');
    entry.classes.forEach(function (klass) {
      if (klass.name === entry.family) {
        sortedChildren(klass.uris).forEach(function (uri) {
          list.append(renderItem(uri, 2));
        });
        return;
      }
      list.append(renderClass(entry.family, klass));
    });
    item.append(list);
    return item;
  }

  function renderClass(family, klass) {
    var key = classKey(family, klass.name);
    var item = renderHeading(key, klass.name, klass.uris.length, 2);
    if (!expanded[key]) return item;
    var list = make('ul');
    list.setAttribute('role', 'group');
    sortedChildren(klass.uris).forEach(function (uri) { list.append(renderItem(uri, 3)); });
    item.append(list);
    return item;
  }

  // A heading is not an element of the model: it carries no URI, cannot be
  // selected, and only folds.
  function renderHeading(key, label, count, level) {
    var item = make('li');
    item.setAttribute('role', 'treeitem');
    item.setAttribute('aria-level', String(level));
    item.setAttribute('aria-expanded', expanded[key] ? 'true' : 'false');
    var row = make('div', 'row heading');
    row.setAttribute('tabindex', '-1');
    var twisty = make('span', 'twisty', expanded[key] ? '\u2212' : '+');
    twisty.addEventListener('click', function (event) {
      event.stopPropagation();
      toggle(key);
    });
    row.append(twisty, make('span', 'glyph', '\u00b7'), make('span', 'cls', label));
    row.append(make('span', 'count', t('beneath', { count: count })));
    row.addEventListener('click', function () { toggle(key); });
    item.append(row);
    return item;
  }

  function familyKey(family) { return '__family__' + family; }

  function classKey(family, name) { return '__class__' + family + '|' + name; }

  function renderItem(uri, level) {
    var entity = index.byUri[uri];
    var children = sortedChildren(index.children[uri] || []);
    var members = entity.grouping ? sortedChildren(Object.keys(index.gathered[uri] || {})) : [];
    var openable = children.length || members.length;
    var item = make('li');
    item.setAttribute('role', 'treeitem');
    item.setAttribute('aria-level', String(level));
    item.dataset.uri = uri;

    var row = make('div', 'row' + (matched[uri] ? '' : ' context'));
    row.dataset.uri = uri;
    row.setAttribute('tabindex', uri === selected ? '0' : '-1');
    if (uri === selected) row.setAttribute('aria-current', 'true');

    var twisty = make('span', 'twisty', openable ? (expanded[uri] ? '\u2212' : '+') : '\u00b7');
    if (openable) {
      item.setAttribute('aria-expanded', expanded[uri] ? 'true' : 'false');
      twisty.addEventListener('click', function (event) {
        event.stopPropagation();
        toggle(uri);
      });
    }
    row.append(twisty, make('span', 'glyph', GLYPHS[entity.kind] || GLYPHS.other));
    row.append(make('span', 'name', entity.name || uri));
    if (entity.class) row.append(make('span', 'cls', entity.class));
    if (entity.grouping && membersOf(uri)) {
      row.append(make('span', 'count', t('gathers', { count: membersOf(uri) })));
    } else if (entity.kind !== 'point' && index.points[uri]) {
      row.append(make('span', 'count', t('points_beneath', { count: index.points[uri] })));
    }
    (entity.findings || []).forEach(function (code) {
      row.append(make('span', 'tag signal', code));
    });
    warningsBeside(entity).forEach(function (code) {
      row.append(make('span', 'tag signal', code));
    });
    var runtime = entity.runtime;
    if (runtime && runtime.outcome && runtime.outcome !== 'active') {
      row.append(make('span', 'tag signal', runtime.outcome));
    } else if (runtime && runtime.outcome) {
      row.append(make('span', 'tag', runtime.outcome));
    }
    row.addEventListener('click', function () { select(uri); });
    row.addEventListener('dblclick', function () { toggle(uri); });
    item.append(row);

    if (expanded[uri] && openable) {
      var list = make('ul');
      list.setAttribute('role', 'group');
      children.forEach(function (child) { list.append(renderItem(child, level + 1)); });
      members.forEach(function (member) { list.append(renderMember(member, level + 1)); });
      item.append(list);
    }
    return item;
  }

  // What a grouping gathers is a view of it and not a place: the rows are
  // dimmed, they hold nothing and count towards nothing, and the element
  // keeps its own place in the building above.
  function renderMember(uri, level) {
    var entity = index.byUri[uri];
    var item = make('li');
    item.setAttribute('role', 'treeitem');
    item.setAttribute('aria-level', String(level));
    var row = make('div', 'row context');
    row.dataset.uri = uri;
    row.setAttribute('tabindex', '-1');
    row.append(make('span', 'twisty', '\u00b7'));
    row.append(make('span', 'glyph', GLYPHS[entity.kind] || GLYPHS.other));
    row.append(make('span', 'name', entity.name || uri));
    if (entity.class) row.append(make('span', 'cls', entity.class));
    row.addEventListener('click', function () { select(uri); });
    item.append(row);
    return item;
  }

  // no_reference is both a finding on every point and the daemon's warning on
  // an accepted one: one name for one condition, so it is shown once.
  function warningsBeside(entity) {
    var findings = entity.findings || [];
    return (entity.warnings || []).filter(function (code) {
      return findings.indexOf(code) < 0;
    });
  }

  function renderUnplaced(children) {
    var item = make('li');
    item.setAttribute('role', 'treeitem');
    item.setAttribute('aria-level', '1');
    item.setAttribute('aria-expanded', expanded.__unplaced__ ? 'true' : 'false');
    var row = make('div', 'row');
    row.setAttribute('tabindex', '-1');
    var twisty = make('span', 'twisty', expanded.__unplaced__ ? '\u2212' : '+');
    twisty.addEventListener('click', function (event) {
      event.stopPropagation();
      toggle('__unplaced__');
    });
    row.append(twisty, make('span', 'glyph', '\u00b7'), make('span', 'name', t('unplaced')));
    row.append(make('span', 'count', t('points_beneath', {
      count: children.reduce(function (total, uri) { return total + index.points[uri]; }, 0)
    })));
    row.addEventListener('click', function () { toggle('__unplaced__'); });
    item.append(row);
    if (expanded.__unplaced__) {
      var list = make('ul');
      list.setAttribute('role', 'group');
      sortedChildren(children).forEach(function (uri) { list.append(renderItem(uri, 2)); });
      item.append(list);
    }
    return item;
  }

  function toggle(uri) {
    expanded[uri] = !expanded[uri];
    renderTree();
  }

  // A collapsed root says nothing, so the building is opened as far as it
  // can be read at a glance: breadth first, until the budget is spent.
  function openTheBuilding() {
    var budget = 120;
    var frontier = index.roots.slice();
    expanded.__unplaced__ = true;
    openTheBand();
    while (frontier.length && budget > 0) {
      var next = [];
      for (var i = 0; i < frontier.length && budget > 0; i++) {
        var children = index.children[frontier[i]] || [];
        if (!children.length) continue;
        expanded[frontier[i]] = true;
        budget -= children.length;
        next = next.concat(children.filter(function (uri) {
          return index.byUri[uri].kind !== 'point';
        }));
      }
      frontier = next;
    }
  }

  function expandAll() {
    var count = 0;
    var next = {};
    Object.keys(index.byUri).forEach(function (uri) {
      if (!visible[uri]) return;
      count += 1;
      if (count <= EXPAND_CAP) next[uri] = true;
    });
    next.__unplaced__ = true;
    expanded = next;
    openTheBand();
    renderTree();
    if (count > EXPAND_CAP) notice(t('expand_capped'));
  }

  // The band is two headings deep and one line per heading, so it is opened
  // whole: folding it away is the point, not folding it out.
  function openTheBand() {
    index.band.forEach(function (entry) {
      expanded[familyKey(entry.family)] = true;
      Object.keys(entry.classes).forEach(function (name) {
        expanded[classKey(entry.family, name)] = true;
      });
    });
  }

  function expandTo(uri) {
    var entity = index.byUri[uri];
    if (entity && entity.grouping) {
      expanded[familyKey(entity.grouping)] = true;
      expanded[classKey(entity.grouping, entity.class || entity.grouping)] = true;
      return;
    }
    var walker = index.parents[uri];
    if (!walker && index.unplaced.indexOf(uri) >= 0) expanded.__unplaced__ = true;
    while (walker) {
      expanded[walker] = true;
      if (!index.parents[walker] && index.unplaced.indexOf(walker) >= 0) {
        expanded.__unplaced__ = true;
      }
      walker = index.parents[walker];
    }
  }

  function onTreeKey(event) {
    var row = event.target.closest ? event.target.closest('.row') : null;
    if (!row) return;
    var rows = Array.prototype.slice.call(el('tree-body').querySelectorAll('.row'));
    var at = rows.indexOf(row);
    var uri = row.dataset.uri;
    var moved = null;
    switch (event.key) {
      case 'ArrowDown': moved = rows[at + 1]; break;
      case 'ArrowUp': moved = rows[at - 1]; break;
      case 'Home': moved = rows[0]; break;
      case 'End': moved = rows[rows.length - 1]; break;
      case 'ArrowRight':
        if (uri && (index.children[uri] || []).length && !expanded[uri]) {
          toggle(uri);
          focusRow(uri);
        } else {
          moved = rows[at + 1];
        }
        break;
      case 'ArrowLeft':
        if (uri && expanded[uri]) {
          toggle(uri);
          focusRow(uri);
        } else if (uri && index.parents[uri]) {
          focusRow(index.parents[uri]);
        }
        break;
      case 'Enter':
      case ' ':
        if (uri) select(uri);
        break;
      case '*':
        expandAll();
        break;
      default:
        return;
    }
    event.preventDefault();
    if (moved) {
      rows.forEach(function (other) { other.setAttribute('tabindex', '-1'); });
      moved.setAttribute('tabindex', '0');
      moved.focus();
    }
  }

  function focusRow(uri) {
    var row = el('tree-body').querySelector('.row[data-uri="' + cssEscape(uri) + '"]');
    if (!row) return;
    el('tree-body').querySelectorAll('.row').forEach(function (other) {
      other.setAttribute('tabindex', '-1');
    });
    row.setAttribute('tabindex', '0');
    row.focus();
    showRow(row);
  }

  // Vertically only: a row is wider than the pane, and scrollIntoView would
  // drag the indentation off the left edge.
  function showRow(row) {
    var body = el('tree-body');
    var left = body.scrollLeft;
    row.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    body.scrollLeft = left;
  }

  function cssEscape(value) {
    return window.CSS && window.CSS.escape ? window.CSS.escape(value) : value.replace(/["\\]/g, '\\$&');
  }

  // --- selection, details and the graph ------------------------------------

  function select(uri, quiet) {
    if (!index.byUri[uri]) return;
    selected = uri;
    expandTo(uri);
    renderTree();
    var row = el('tree-body').querySelector('.row[data-uri="' + cssEscape(uri) + '"]');
    if (row && !quiet) showRow(row);
    window.history.replaceState(null, '', window.location.pathname + window.location.search + '#' + encodeURIComponent(uri));
    renderDetails();
    renderGraph();
  }

  // The same shape the rest of the interface shows: YYYY-MM-DD HH:MM:SS in UTC.
  function formatTime(value) {
    if (!value) return '';
    var stamp = new Date(value);
    if (isNaN(stamp.getTime())) return String(value);
    return stamp.toISOString().slice(0, 19).replace('T', ' ');
  }

  // A number keeps its unit beside it; a text the source gave stands alone.
  function formatValue(value, unit) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'number' && unit) {
      return String(value) + ' ' + unit.replace(/^unit:/, '');
    }
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    return String(value);
  }

  function renderDetails() {
    var entity = index.byUri[selected];
    var box = el('details');
    if (!entity) {
      box.replaceChildren(make('p', 'empty', t('select')));
      return;
    }
    var list = make('dl', 'kv');
    function pair(label, node) {
      list.append(make('dt', null, label));
      var dd = make('dd');
      if (typeof node === 'string') dd.textContent = node;
      else if (Array.isArray(node)) node.forEach(function (part) { dd.append(part); });
      else dd.append(node);
      list.append(dd);
    }
    var copy = make('button', 'button quiet', t('copy'));
    copy.type = 'button';
    copy.addEventListener('click', function () { copyText(entity.uri, copy); });
    pair(t('uri'), [document.createTextNode(entity.uri), copy]);
    pair(t('name'), entity.name || '');
    pair(t('kind'), t('kind.' + entity.kind));
    if (entity.class) pair(t('class'), entity.class);
    if ((entity.types || []).length > 1) pair(t('types'), entity.types.join(', '));
    if (entity.unit) pair(t('unit'), entity.unit);
    if ((entity.references || []).length) pair(t('references'), entity.references.join(', '));

    var parts = [list];
    if ((entity.findings || []).length) {
      var findings = make('div');
      findings.append(make('h3', null, t('findings')));
      entity.findings.forEach(function (code) {
        var line = make('div', 'finding');
        line.append(make('span', 'tag signal', code));
        line.append(make('small', null, t('finding.' + code)));
        findings.append(line);
      });
      parts.push(findings);
    }
    if (entity.last_known_value) {
      var reading = make('div');
      reading.append(make('h3', null, t('last_value')));
      var shown = make('dl', 'kv');
      shown.append(make('dt', null, t('value')),
        make('dd', 'mono', formatValue(entity.last_known_value.value, entity.unit)));
      shown.append(make('dt', null, t('observed')),
        make('dd', 'mono', formatTime(entity.last_known_value.time)));
      reading.append(shown);
      parts.push(reading);
    }
    if (entity.runtime) {
      var runtime = make('div');
      runtime.append(make('h3', null, t('runtime')));
      var facts = make('dl', 'kv');
      if (entity.runtime.outcome) {
        facts.append(make('dt', null, t('filter.outcome')), make('dd', null, entity.runtime.outcome));
        facts.append(make('dt', null, t('filter.instance')), make('dd', null, entity.runtime.instance || ''));
        if (entity.runtime.method) {
          facts.append(make('dt', null, 'method'), make('dd', null, entity.runtime.method + (entity.runtime.fallback_active ? ' (fallback)' : '')));
        }
      } else {
        facts.append(make('dt', null, t('filter.outcome')),
          make('dd', null, entity.runtime.accepted ? t('accepted') : t('not_accepted')));
      }
      runtime.append(facts);
      parts.push(runtime);
    }
    var beside = warningsBeside(entity);
    if (beside.length) {
      var warnings = make('div');
      warnings.append(make('h3', null, t('warnings')));
      beside.forEach(function (code) { warnings.append(make('span', 'tag signal', code)); });
      parts.push(warnings);
    }
    parts.push(relationList(t('relations_out'), index.out[entity.uri] || [], 'object'));
    parts.push(relationList(t('relations_in'), index.into[entity.uri] || [], 'subject'));

    var actions = make('div', 'button-row');
    if (entity.kind === 'point' && entity.class) {
      var points = make('a', 'button quiet', t('show_points'));
      points.href = CONFIG.pointsUrl + '?class=' + encodeURIComponent(entity.class);
      actions.append(points);
    }
    var query = make('a', 'button quiet', t('open_query'));
    query.href = CONFIG.queryUrl + '?query=' + encodeURIComponent('DESCRIBE ' + entity.uri);
    actions.append(query);
    parts.push(actions);
    box.replaceChildren.apply(box, parts.filter(Boolean));
  }

  function relationList(title, relations, end) {
    if (!relations.length) return null;
    var box = make('div');
    box.append(make('h3', null, title));
    var list = make('ul', 'relations');
    relations.forEach(function (relation) {
      var other = relation[end];
      var item = make('li');
      item.append(make('span', 'pred', relation.predicate));
      var link = make('button', 'linklike', (index.byUri[other] || {}).name || other);
      link.type = 'button';
      link.addEventListener('click', function () { select(other); });
      item.append(link);
      list.append(item);
    });
    box.append(list);
    return box;
  }

  function copyText(value, button) {
    var done = function () {
      var was = button.textContent;
      button.textContent = t('copied');
      window.setTimeout(function () { button.textContent = was; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done, function () { /* nothing to do */ });
    }
  }

  function neighbourhood(uri, depth) {
    var levels = {}, order = [uri];
    levels[uri] = 0;
    for (var i = 0; i < order.length; i++) {
      var at = order[i];
      if (levels[at] >= depth) continue;
      var touching = (index.out[at] || []).concat(index.into[at] || []);
      touching.forEach(function (relation) {
        [relation.subject, relation.object].forEach(function (other) {
          if (levels[other] === undefined) {
            levels[other] = levels[at] + 1;
            order.push(other);
          }
        });
      });
    }
    return { levels: levels, uris: order };
  }

  function renderGraph() {
    var container = el('graph');
    if (!selected || !window.cytoscape || !container) return;
    var asked = parseInt(state.depth || '1', 10);
    var found = neighbourhood(selected, asked);
    var shown = asked;
    while (shown > 1 && found.uris.length > GRAPH_CAP) {
      shown -= 1;
      found = neighbourhood(selected, shown);
    }
    if (found.uris.length > GRAPH_CAP) {
      found.uris = found.uris.slice(0, GRAPH_CAP);
    }
    var notice = el('graph-notice');
    if (shown !== asked || found.uris.length >= GRAPH_CAP) {
      notice.textContent = t('capped', { total: found.uris.length, asked: asked, shown: shown });
      notice.hidden = false;
    } else {
      notice.hidden = true;
    }

    var kept = {};
    found.uris.forEach(function (uri) { kept[uri] = true; });
    var elements = found.uris.map(function (uri) {
      var entity = index.byUri[uri];
      return {
        data: {
          id: uri,
          label: entity.name || uri,
          kind: entity.kind,
          flagged: (entity.findings || []).length || (entity.warnings || []).length ? 'yes' : 'no',
          depth: found.levels[uri]
        }
      };
    });
    var seen = {};
    found.uris.forEach(function (uri) {
      (index.out[uri] || []).forEach(function (relation) {
        if (!kept[relation.subject] || !kept[relation.object]) return;
        var id = relation.subject + '|' + relation.predicate + '|' + relation.object;
        if (seen[id]) return;
        seen[id] = true;
        elements.push({
          data: {
            id: id, source: relation.subject, target: relation.object,
            label: relation.predicate, contains: relation.role ? 'yes' : 'no'
          }
        });
      });
    });

    if (cy) cy.destroy();
    cy = window.cytoscape({
      container: container,
      elements: elements,
      style: graphStyle(),
      layout: {
        name: 'concentric',
        animate: false,
        concentric: function (node) { return 10 - node.data('depth'); },
        levelWidth: function () { return 1; },
        minNodeSpacing: 70,
        padding: 24
      },
      wheelSensitivity: WHEEL_SENSITIVITY
    });
    cy.$id(selected).addClass('chosen');
    cy.on('tap', 'node', function (event) { select(event.target.id()); });
    cy.on('dbltap', 'node', function (event) {
      selected = event.target.id();
      renderGraph();
    });
    container.setAttribute('aria-label', t('graph.label', {
      name: (index.byUri[selected] || {}).name || selected, count: found.uris.length
    }));
  }

  function graphStyle() {
    var ink = cssVar('--ink', '#141414');
    var dim = cssVar('--dim', '#6E6963');
    var paper = cssVar('--paper', '#FFFFFF');
    var signal = cssVar('--signal', '#E35D28');
    var sans = cssVar('--sans', 'sans-serif');
    var mono = cssVar('--mono', 'monospace');
    return [
      {
        selector: 'node',
        style: {
          'background-color': paper, 'border-color': ink, 'border-width': 1,
          shape: function (node) { return SHAPES[node.data('kind')] || 'rectangle'; },
          label: 'data(label)', 'font-family': sans, 'font-size': 11, color: ink,
          'text-valign': 'bottom', 'text-margin-y': 4, 'text-wrap': 'ellipsis',
          'text-max-width': 120, width: 26, height: 26
        }
      },
      { selector: 'node[kind = "other"]', style: { 'border-style': 'dashed' } },
      { selector: 'node[flagged = "yes"]', style: { 'border-color': signal, color: signal } },
      { selector: 'node.chosen', style: { 'background-color': ink, 'border-color': ink, 'border-width': 2 } },
      {
        selector: 'edge',
        style: {
          width: 1, 'line-color': dim, 'target-arrow-color': dim,
          'target-arrow-shape': 'triangle', 'arrow-scale': 0.7, 'curve-style': 'bezier',
          label: 'data(label)', 'font-family': mono, 'font-size': 9, color: dim,
          'text-rotation': 'autorotate', 'text-background-color': paper,
          'text-background-opacity': 1, 'text-background-padding': 2
        }
      },
      { selector: 'edge[contains = "no"]', style: { 'line-style': 'dashed' } }
    ];
  }

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (value || '').trim() || fallback;
  }

  function exportPng() {
    if (!cy) return;
    var uri = cy.png({ full: true, scale: 2, bg: cssVar('--paper', '#FFFFFF') });
    var link = make('a');
    link.href = uri;
    link.download = 'explorer-' + String(selected).replace(/[^\w.-]+/g, '-') + '.png';
    document.body.append(link);
    link.click();
    link.remove();
  }

  // --- notices and the version watch ---------------------------------------

  function notice(message, signal) {
    var box = el('notice');
    if (!box) return;
    box.className = signal ? 'notice signal' : 'notice';
    box.replaceChildren(document.createTextNode(message + ' '));
    var again = make('button', 'linklike', t('reload'));
    again.type = 'button';
    again.addEventListener('click', loadModel);
    box.append(again);
    box.hidden = false;
  }

  function clearNotice() {
    var box = el('notice');
    if (box) box.hidden = true;
  }

  function onStatusSwap(event) {
    if (!event.target || event.target.id !== 'statusline') return;
    var marked = event.target.querySelector('[data-model]');
    if (!marked) return;  // the daemon is away; the status line says so itself
    var version = marked.dataset.model;
    if (version && model && String(version) !== String(model.version)) {
      notice(t('version.changed', { version: version }), false);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
