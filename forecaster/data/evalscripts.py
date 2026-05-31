from .scripts import CDOM_Se2WaQ, Chl_a_Se2WaQ, DOC_Se2WaQ, Turb_Se2WaQ, Cya_Se2WaQ
from .scripts import WQI, WQI2

cdom = CDOM_Se2WaQ.se2waq
chl_a = Chl_a_Se2WaQ.se2waq
doc = DOC_Se2WaQ.se2waq
turb = Turb_Se2WaQ.se2waq
cya = Cya_Se2WaQ.se2waq

wqi = WQI.wqi
wqi2 = WQI2.wqi2

true_color_optimized = """
//VERSION=3
function setup() {
  return {
    input: ["B04", "B03", "B02", "dataMask"],
    output: { bands: 4 }
  };
}

const maxR = 3.0;
const midR = 0.13;
const sat = 1.2;
const gamma = 1.8;

function evaluatePixel(smp) {
  const rgbLin = satEnh(sAdj(smp.B04), sAdj(smp.B03), sAdj(smp.B02));
  return [sRGB(rgbLin[0]), sRGB(rgbLin[1]), sRGB(rgbLin[2]), smp.dataMask];
}

function sAdj(a) {
  return adjGamma(adj(a, midR, 1, maxR));
}

const gOff = 0.01;
const gOffPow = Math.pow(gOff, gamma);
const gOffRange = Math.pow(1 + gOff, gamma) - gOffPow;

function adjGamma(b) {
  return (Math.pow((b + gOff), gamma) - gOffPow) / gOffRange;
}

function satEnh(r, g, b) {
  const avgS = (r + g + b) / 3.0 * (1 - sat);
  return [clip(avgS + r * sat), clip(avgS + g * sat), clip(avgS + b * sat)];
}

function clip(s) {
  return s < 0 ? 0 : s > 1 ? 1 : s;
}

function adj(a, tx, ty, maxC) {
  var ar = clip(a / maxC, 0, 1);
  return ar * (ar * (tx / maxC + ty - 1) - ty) / (ar * (2 * tx / maxC - 1) - tx / maxC);
}

const sRGB = (c) => c <= 0.0031308 ? (12.92 * c) : (1.055 * Math.pow(c, 0.41666666666) - 0.055);
"""

surface_temperature = """
//VERSION=3
var option = 0;
var minC = 0;
var maxC = 50;
var NDVIs = 0.2;
var NDVIv = 0.8;
var waterE = 0.991;
var soilE = 0.966;
var vegetationE = 0.973;
var C = 0.009;
var bCent = 0.000010854;
var rho = 0.01438;

let viz = ColorRampVisualizer.createRedTemperature(minC, maxC);

function setup() {
  return {
    input: [
      { datasource: "S3SLSTR", bands: ["S8"] },
      { datasource: "S3OLCI", bands: ["B06", "B08", "B17"] }
    ],
    output: [{ id: "default", bands: 3, sampleType: SampleType.AUTO }],
    mosaicking: "ORBIT"
  };
}

function LSEcalc(NDVI, Pv) {
  var LSE;
  if (NDVI < 0) {
    LSE = waterE;
  } else if (NDVI < NDVIs) {
    LSE = soilE;
  } else if (NDVI > NDVIv) {
    LSE = vegetationE;
  } else {
    LSE = vegetationE * Pv + soilE * (1 - Pv) + C;
  }
  return LSE;
}

function evaluatePixel(samples) {
  var LSTmax = -999;
  var LSTavg = 0;
  var reduceNavg = 0;
  var N = samples.S3SLSTR.length;

  for (let i = 0; i < N; i++) {
    var Bi = samples.S3SLSTR[i].S8;
    var B06i = samples.S3OLCI[i].B06;
    var B08i = samples.S3OLCI[i].B08;
    var B17i = samples.S3OLCI[i].B17;

    if (Bi > 173 && Bi < 65000 && B06i > 0 && B08i > 0 && B17i > 0) {
      var S8BTi = Bi - 273.15;
      var NDVIi = (B17i - B08i) / (B17i + B08i);
      var PVi = Math.pow((NDVIi - NDVIs) / (NDVIv - NDVIs), 2);
      var LSEi = LSEcalc(NDVIi, PVi);
      var LSTi = S8BTi / (1 + ((bCent * S8BTi) / rho) * Math.log(LSEi));

      LSTavg = LSTavg + LSTi;
      if (LSTi > LSTmax) {
        LSTmax = LSTi;
      }
    } else {
      ++reduceNavg;
    }
  }

  N = N - reduceNavg;
  if (N <= 0) {
    return [0, 0, 0];
  }

  LSTavg = LSTavg / N;
  let outLST = option == 0 ? LSTavg : LSTmax;
  return viz.process(outLST);
}
"""
