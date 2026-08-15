/* tinyQR — minimal self-contained QR code generator (byte mode, EC level M,
 * versions 1-10, all 8 masks with penalty selection). No external deps.
 * Written for DSH Cloud to render WeChat Pay code_url on a canvas.
 * Usage: window.tinyQR.draw(canvasEl, "weixin://wxpay/...", 220)
 */
(function () {
  "use strict";

  // --- GF(256) ---------------------------------------------------------------
  var EXP = new Array(512), LOG = new Array(256);
  (function () {
    var x = 1;
    for (var i = 0; i < 255; i++) {
      EXP[i] = x;
      LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11d;
    }
    for (i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  })();

  function gmul(a, b) { return (a && b) ? EXP[LOG[a] + LOG[b]] : 0; }

  function rsEC(data, ecCount) {
    var gen = [1];
    for (var i = 0; i < ecCount; i++) {
      var next = new Array(gen.length + 1);
      for (var k = 0; k < next.length; k++) next[k] = 0;
      for (var j = 0; j < gen.length; j++) {
        next[j] ^= gen[j];
        next[j + 1] ^= gmul(gen[j], EXP[i]);
      }
      gen = next;
    }
    var res = data.slice();
    for (i = 0; i < ecCount; i++) res.push(0);
    for (i = 0; i < data.length; i++) {
      var f = res[i];
      if (f) for (j = 0; j < gen.length; j++) res[i + j] ^= gmul(gen[j], f);
    }
    return res.slice(data.length);
  }

  // --- version tables (EC level M) ------------------------------------------
  // version: [ecPerBlock, [group1Blocks, group1DataCW], [group2Blocks, group2DataCW]]
  var SPEC = {
    1: [10, [1, 16], [0, 0]],
    2: [16, [1, 28], [0, 0]],
    3: [26, [1, 44], [0, 0]],
    4: [18, [2, 32], [0, 0]],
    5: [24, [2, 43], [0, 0]],
    6: [16, [4, 27], [0, 0]],
    7: [18, [4, 31], [0, 0]],
    8: [22, [2, 38], [2, 39]],
    9: [22, [3, 36], [2, 37]],
    10: [26, [4, 43], [1, 44]]
  };
  var ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]
  };

  function dataCodewords(v) {
    var s = SPEC[v];
    return s[1][0] * s[1][1] + s[2][0] * s[2][1];
  }

  function byteCapacity(v) {
    var headerBits = 4 + (v <= 9 ? 8 : 16);
    return Math.floor((dataCodewords(v) * 8 - headerBits) / 8);
  }

  // --- BCH for format / version info ----------------------------------------
  function bch15_5(v) {
    var d = v << 10;
    for (var i = 14; i >= 10; i--) if ((d >>> i) & 1) d ^= 0x537 << (i - 10);
    return ((v << 10) | d) ^ 0x5412;
  }

  function bch18_6(v) {
    var d = v << 12;
    for (var i = 17; i >= 12; i--) if ((d >>> i) & 1) d ^= 0x1f25 << (i - 12);
    return (v << 12) | d;
  }

  // --- encoding --------------------------------------------------------------
  function toUtf8(str) {
    if (typeof TextEncoder !== "undefined") return Array.prototype.slice.call(new TextEncoder().encode(str));
    var out = [], enc = unescape(encodeURIComponent(str));
    for (var i = 0; i < enc.length; i++) out.push(enc.charCodeAt(i) & 0xff);
    return out;
  }

  function makeCodewords(bytes, version) {
    var bits = [];
    function push(val, len) { for (var i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); }
    push(4, 4); // byte mode
    push(bytes.length, version <= 9 ? 8 : 16);
    for (var i = 0; i < bytes.length; i++) push(bytes[i], 8);
    var capBits = dataCodewords(version) * 8;
    push(0, Math.min(4, capBits - bits.length)); // terminator
    while (bits.length % 8) bits.push(0);
    var data = [];
    for (i = 0; i < bits.length; i += 8) {
      var b = 0;
      for (var j = 0; j < 8; j++) b = (b << 1) | bits[i + j];
      data.push(b);
    }
    var pads = [0xec, 0x11], p = 0;
    while (data.length < dataCodewords(version)) data.push(pads[p++ % 2]);

    // split into blocks + compute EC
    var spec = SPEC[version], ec = spec[0];
    var blocks = [], ecBlocks = [], off = 0;
    [spec[1], spec[2]].forEach(function (g) {
      for (var n = 0; n < g[0]; n++) {
        var blk = data.slice(off, off + g[1]);
        off += g[1];
        blocks.push(blk);
        ecBlocks.push(rsEC(blk, ec));
      }
    });
    // interleave
    var out = [];
    var maxLen = Math.max(spec[1][1], spec[2][1]);
    for (i = 0; i < maxLen; i++) {
      for (j = 0; j < blocks.length; j++) if (i < blocks[j].length) out.push(blocks[j][i]);
    }
    for (i = 0; i < ec; i++) {
      for (j = 0; j < ecBlocks.length; j++) out.push(ecBlocks[j][i]);
    }
    return out;
  }

  // --- matrix ----------------------------------------------------------------
  function buildMatrix(version, codewords, maskPattern) {
    var size = 17 + version * 4;
    var m = [];
    for (var r = 0; r < size; r++) {
      m.push([]);
      for (var c = 0; c < size; c++) m[r].push(null);
    }

    function probe(row, col) {
      for (var dr = -1; dr <= 7; dr++) {
        if (row + dr < 0 || row + dr >= size) continue;
        for (var dc = -1; dc <= 7; dc++) {
          if (col + dc < 0 || col + dc >= size) continue;
          m[row + dr][col + dc] =
            (dr >= 0 && dr <= 6 && (dc === 0 || dc === 6)) ||
            (dc >= 0 && dc <= 6 && (dr === 0 || dr === 6)) ||
            (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4);
        }
      }
    }
    probe(0, 0);
    probe(size - 7, 0);
    probe(0, size - 7);

    var pos = ALIGN[version];
    for (var i = 0; i < pos.length; i++) {
      for (var j = 0; j < pos.length; j++) {
        var row = pos[i], col = pos[j];
        if (m[row][col] !== null) continue;
        for (var dr2 = -2; dr2 <= 2; dr2++) {
          for (var dc2 = -2; dc2 <= 2; dc2++) {
            m[row + dr2][col + dc2] =
              dr2 === -2 || dr2 === 2 || dc2 === -2 || dc2 === 2 || (dr2 === 0 && dc2 === 0);
          }
        }
      }
    }

    for (r = 8; r < size - 8; r++) if (m[r][6] === null) m[r][6] = r % 2 === 0;
    for (c = 8; c < size - 8; c++) if (m[6][c] === null) m[6][c] = c % 2 === 0;

    // format info (EC level M = 0b00)
    var fbits = bch15_5((0 << 3) | maskPattern);
    for (i = 0; i < 15; i++) {
      var mod = ((fbits >> i) & 1) === 1;
      var fr = i < 6 ? i : i < 8 ? i + 1 : size - 15 + i;
      m[fr][8] = mod;
      var fc = i < 8 ? size - 1 - i : i < 9 ? 15 - i : 14 - i;
      m[8][fc] = mod;
    }
    m[size - 8][8] = true; // dark module

    if (version >= 7) {
      var vbits = bch18_6(version);
      for (i = 0; i < 18; i++) {
        mod = ((vbits >> i) & 1) === 1;
        m[Math.floor(i / 3)][(i % 3) + size - 11] = mod;
        m[(i % 3) + size - 11][Math.floor(i / 3)] = mod;
      }
    }

    // data placement (zigzag)
    function masked(mask, r2, c2) {
      switch (mask) {
        case 0: return (r2 + c2) % 2 === 0;
        case 1: return r2 % 2 === 0;
        case 2: return c2 % 3 === 0;
        case 3: return (r2 + c2) % 3 === 0;
        case 4: return (Math.floor(r2 / 2) + Math.floor(c2 / 3)) % 2 === 0;
        case 5: return ((r2 * c2) % 2) + ((r2 * c2) % 3) === 0;
        case 6: return (((r2 * c2) % 2) + ((r2 * c2) % 3)) % 2 === 0;
        default: return (((r2 + c2) % 2) + ((r2 * c2) % 3)) % 2 === 0;
      }
    }

    var inc = -1, row2 = size - 1, bitIdx = 7, byteIdx = 0;
    for (var col2 = size - 1; col2 > 0; col2 -= 2) {
      if (col2 === 6) col2--;
      for (;;) {
        for (var cc = 0; cc < 2; cc++) {
          if (m[row2][col2 - cc] === null) {
            var dark = false;
            if (byteIdx < codewords.length) dark = ((codewords[byteIdx] >>> bitIdx) & 1) === 1;
            if (masked(maskPattern, row2, col2 - cc)) dark = !dark;
            m[row2][col2 - cc] = dark;
            bitIdx--;
            if (bitIdx === -1) { byteIdx++; bitIdx = 7; }
          }
        }
        row2 += inc;
        if (row2 < 0 || row2 >= size) { row2 -= inc; inc = -inc; break; }
      }
    }
    return m;
  }

  function penalty(m) {
    var size = m.length, score = 0, r, c;
    // rule 1: runs of same color >= 5
    for (var dir = 0; dir < 2; dir++) {
      for (r = 0; r < size; r++) {
        var run = 1;
        for (c = 1; c < size; c++) {
          var cur = dir ? m[c][r] : m[r][c];
          var prev = dir ? m[c - 1][r] : m[r][c - 1];
          if (cur === prev) {
            run++;
            if (c === size - 1 && run >= 5) score += 3 + run - 5;
          } else {
            if (run >= 5) score += 3 + run - 5;
            run = 1;
          }
        }
      }
    }
    // rule 2: 2x2 blocks
    for (r = 0; r < size - 1; r++) {
      for (c = 0; c < size - 1; c++) {
        if (m[r][c] === m[r][c + 1] && m[r][c] === m[r + 1][c] && m[r][c] === m[r + 1][c + 1]) score += 3;
      }
    }
    // rule 3: finder-like patterns
    var pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1];
    function at(dir2, a, b) { return (dir2 ? m[b][a] : m[a][b]) ? 1 : 0; }
    for (dir = 0; dir < 2; dir++) {
      for (r = 0; r < size; r++) {
        for (c = 0; c + 11 <= size; c++) {
          var m1 = true, m2 = true;
          for (var k = 0; k < 11; k++) {
            var v = at(dir, r, c + k);
            if (v !== pat1[k]) m1 = false;
            if (v !== pat2[k]) m2 = false;
          }
          if (m1) score += 40;
          if (m2) score += 40;
        }
      }
    }
    // rule 4: dark proportion
    var darkCount = 0;
    for (r = 0; r < size; r++) for (c = 0; c < size; c++) if (m[r][c]) darkCount++;
    var pct = (darkCount * 100) / (size * size);
    score += Math.floor(Math.abs(pct - 50) / 5) * 10;
    return score;
  }

  function makeMatrix(text) {
    var bytes = toUtf8(text);
    var version = 0;
    for (var v = 1; v <= 10; v++) {
      if (bytes.length <= byteCapacity(v)) { version = v; break; }
    }
    if (!version) throw new Error("tinyQR: content too long (" + bytes.length + " bytes)");
    var codewords = makeCodewords(bytes, version);
    var best = null, bestScore = Infinity;
    for (var mask = 0; mask < 8; mask++) {
      var m = buildMatrix(version, codewords, mask);
      var s = penalty(m);
      if (s < bestScore) { bestScore = s; best = m; }
    }
    return best;
  }

  function draw(canvas, text, sizePx) {
    var m = makeMatrix(text);
    var n = m.length, quiet = 4;
    var px = sizePx || canvas.width || 220;
    canvas.width = px;
    canvas.height = px;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, px, px);
    var scale = px / (n + quiet * 2);
    ctx.fillStyle = "#000000";
    for (var r = 0; r < n; r++) {
      for (var c = 0; c < n; c++) {
        if (m[r][c]) {
          var x = Math.floor((c + quiet) * scale), y = Math.floor((r + quiet) * scale);
          var w = Math.ceil((c + 1 + quiet) * scale) - x, h = Math.ceil((r + 1 + quiet) * scale) - y;
          ctx.fillRect(x, y, w, h);
        }
      }
    }
  }

  window.tinyQR = { draw: draw, matrix: makeMatrix };
})();
