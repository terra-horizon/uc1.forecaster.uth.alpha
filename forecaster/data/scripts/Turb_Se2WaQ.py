
# Sentinel-2 Water Qualitiy Script
se2waq = """
//VERSION 3

var FLAGparam = 2;
var FLAGbackGround = 0;

// Water-land contrast index (to define the background)

var NDWI = index(B03, B08); 

// Background indexes                           

var Black = [0];                                       // FLAGbackGround = 0

var NDVI = index(B08, B04);                            // FLAGbackGround = 1

var TrueColor = [B04*2.5, B03*2.5, B02*2.5];           // FLAGbackGround = 2


// Empirical models

if (B01 == 0) { 
  var Turb = 0;                                          // FLAGparam = 2; S2-L2A; [1] Unit: NTU;          
} else {
  var Turb = 8.93 * (B03/B01) - 6.39;                    // FLAGparam = 2; S2-L1C; [1] Unit: NTU;          
}


// Numerical values for the scales of parameters

var scaleTurb  = [0, 4, 8, 12, 16, 20];


// Colors for the scales

var s = 255;
var colorScale = 
  [
   [73/s, 111/s, 242/s],
   [130/s, 211/s, 95/s],
   [254/s, 253/s, 5/s],
   [253/s, 0/s, 4/s],
   [142/s, 32/s, 38/s],
   [217/s, 124/s, 245/s]
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
  if (B01 === 0 || isNaN(B03/B01)) {
    return [0.5, 0.5, 0.5]; // Gray for no data
  } else {
    switch ( FLAGparam ) {
      case 2:
        return colorBlend(Turb, scaleTurb, colorScale);
        break;
      default:
        return TrueColor;
    }
  }
}
"""