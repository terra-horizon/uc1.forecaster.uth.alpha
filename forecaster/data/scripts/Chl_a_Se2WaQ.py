# Sentinel-2 Water Qualitiy Script
se2waq = """
//VERSION 3

var FLAGparam = 0;
var FLAGbackGround = 0;

// Water-land contrast index (to define the background)

var NDWI = index(B03, B08); 

// Background indexes                           

var Black = [0];                                       // FLAGbackGround = 0

var NDVI = index(B08, B04);                            // FLAGbackGround = 1

var TrueColor = [B04*2.5, B03*2.5, B02*2.5];           // FLAGbackGround = 2


// Empirical models
if (B01 == 0 || B03 == 0) {
  var Chl_a = 0;                                         // FLAGparam = 0; S2-L1C; [1] Unit: mg/m3;
} else { 
  var Chl_a = 4.26 * Math.pow(B03/B01, 3.94);            // FLAGparam = 0; S2-L2A; [1] Unit: mg/m3;            
}

// Numerical values for the scales of parameters

var scaleChl_a = [0, 6, 12, 20, 30, 50];

// Colors for the scales

var s = 255;
var colorScale = [
  [73/s, 111/s, 242/s],   // Blue (0)
  [130/s, 211/s, 95/s],   // Green (6)
  [254/s, 253/s, 5/s],    // Yellow (12)
  [253/s, 0/s, 4/s],      // Red (20)
  [142/s, 32/s, 38/s],    // Dark Red (30)
  [73/s, 111/s, 242/s]   // Blue (0)

];

// Image generation

if (NDWI<0) {
  if ( FLAGbackGround == 0 ) {
    return Black;
  } else if ( FLAGbackGround == 1 ) {
    return [0, .5*(NDVI+1), 0];
  } else if ( FLAGbackGround == 2 ) {
    return TrueColor;
  }
} else {
  if (B01 === 0 || B03 === 0 || isNaN(B03/B01)) {
    return [0.5, 0.5, 0.5]; // Gray for no data
  } else {
    switch ( FLAGparam ) {
      case 0:
      return colorBlend(Chl_a, scaleChl_a, colorScale);
      break;
      default:
        return Black; 
    }
  }
}
"""