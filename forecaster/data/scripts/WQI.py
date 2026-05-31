wqi = """
//VERSION=3
function setup() {
  return {
    input: [{
      bands: [
        "B01", // Required for Chl_a, Cya, Turb, etc.
        "B02", // Required for Cya, TrueColor, etc.
        "B03", // Required for NDWI, Chl_a, Cya, etc.
        "B04", // Required for NDVI, CDOM, DOC, etc.
        "B05", 
        "B08", // Required for NDWI, NDVI
        "SCL",  // Scene Classification Layer (optional for masking)
        "dataMask" // Data mask to exclude nodata pixels
      ]
    }],
    output: [
      {
        id: "data",
        bands: 6
      },
      {
        id: "dataMask",
        bands: 1
      }]
  }
}

function evaluatePixel(samples) {
    let ndvi = (samples.B08 - samples.B04)/(samples.B08 + samples.B04)

    var validNDVIMask = 1
    if (samples.B08 + samples.B04 == 0 ){
        validNDVIMask = 0
    }
    var waterMask = 0;
    if (samples.SCL == 6) {
        waterMask = 1;
    }

    

    return {
        data: [samples.B01, samples.B02, samples.B03, samples.B04,samples.B05, samples.B08],
        // Include only water pixels in statistics:
        dataMask: [samples.dataMask * waterMask]
    };
}
"""
