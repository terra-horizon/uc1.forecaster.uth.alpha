se2waq = """
//VERSION 3

var FLAGparam = 4;
var FLAGbackGround = 0;

// Water-land contrast index (to define the background)

var NDWI = index(B03, B08); 

// Background indexes                           

var Black = [0];                                       // FLAGbackGround = 0

var NDVI = index(B08, B04);                            // FLAGbackGround = 1

var TrueColor = [B04*2.5, B03*2.5, B02*2.5];           // FLAGbackGround = 2


// Empirical models        

if (B04 == 0) {
  var DOC = 0;                                         // FLAGparam = 4; S2-L2A; [2] Unit: mg/l; 
} else { 
  var DOC = 432 * Math.exp(-2.24*B03/B04);               // FLAGparam = 4; S2-L1C; [2] Unit: mg/l;         
}
     


// Numerical values for the scales of parameters

var scaleDOC   = [0, 5, 10, 20, 30, 40];


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
  if (B04 === 0 || isNaN(B03/B04)) {
    return [0.5, 0.5, 0.5]; // Gray for no data
  } else {
    switch ( FLAGparam ) {
      case 4:
        return colorBlend(DOC, scaleDOC, colorScale);
        break;
      default:
        return TrueColor;
    }
  }
}

"""