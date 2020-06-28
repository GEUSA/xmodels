const csv = require('csvtojson');
const fs = require('fs');
const config = require('./config');
const path = require('path');

let csvFile = path.join(__dirname, '../', 'ge-props.csv');
let allCategories = [];

const getImageLinkXml = (imageLinks) => {
  const xmlLinks = [];

  imageLinks.forEach((link) => {
    if (link.trim() !== '') {
      xmlLinks.push(`<imagefile><![CDATA[${link}]]></imagefile>`);
    }
  });

  if (xmlLinks.length > 0) {
    return xmlLinks.join('\n');
  } else {
    return '<imagefile/>';
  }
};

const getModelCategoryXml = (categories) => {
  const categoryToXml = (categoryId) => {
    return `<categoryid>${categoryId}</categoryid>`;
  };

  const categoriesXml = [];

  categories.forEach((cat) => {
    if (cat !== '') {
      const catId = allCategories.indexOf(cat);
      categoriesXml.push(categoryToXml(catId));
    }
  });

  return categoriesXml.join('\n');
};

const createCategories = (models) => {
  models.forEach((model) => {
    const categories = model.category;
    if (categories) {
      categories.forEach((cat) => {
        cat = cat.trim();
        if (cat !== '' && allCategories.indexOf(cat) === -1) {
          allCategories.push(cat);
        }
      });
    }
  });

  allCategories.sort();

  const categoriesXml = [];

  allCategories.forEach((cat, index) => {
    categoriesXml.push(`<category><id>${index}</id><name>${cat}</name></category>`);
  });

  return categoriesXml.join('\n');
};

const modelXML = (data) => {
  let xmodelLink = '<wiring/>';
  let xmlNote = '<notes/>';

  if (data.xmodelLink) {
    xmodel = ['<wiring>', '<xmodellink>'];
    xmodel.push(`<![CDATA[${data.xmodelLink}]]>`);
    xmodel.push('</xmodellink>');
    xmodel.push('</wiring>');
    xmodelLink = xmodel.join('\n');
  }

  if (data.notes !== '') {
    xmlNote = `<notes>${data.notes}</notes>`;
  }

  const xml = [
    '<model>',
    `<id>${data.id}</id>`,
    `${data.categories}`,
    `<name>${data.name}</name>`,
    `<type>${data.type}</type>`,
    `<weblink><![CDATA[${data.gelink}]]></weblink>`,
    `<material>${data.material}</material>`,
    `<width>${data.width}</width>`,
    `<height>${data.height}</height>`,
    `<thickness>${data.thickness}</thickness>`,
    `<pixelcount>${data.pixelcount}</pixelcount>`,
    `<pixeldescription>12mm bullet</pixeldescription>`,
    `<pixelspacing>0" (cm)</pixelspacing>`,
    data.imageLinks,
    xmodelLink,
    xmlNote,
    `</model>`,
  ];

  return xml.join('\n');
};

const buildDimensionString = (dimension) => {
  const dimensions = [];

  dimension.forEach((d) => {
    if (d) {
      const dString = `${d}" (${Math.round(d * 2.54)}cm)`;
      dimensions.push(dString);
    }
  });

  return dimensions.join(' or ');
};

const parseModels = (modelData) => {
  const parsedModels = [];

  modelData.forEach((model) => {
    const m4x = {
      name: null,
      categories: null,
      type: 'GE Prop',
      gelink: null,
      material: 'Coro',
      width: '" (cm)',
      height: '" (cm)',
      thickness: '12mm',
      pixelcount: 0,
      pixeldescription: '12mm bullets',
      pixelspacing: '0" (cm)',
      notes: '',
      xmodelLink: null,
      imageLinks: '',
    };

    m4x.name = model.option ? `${model.prop} - ${model.option}` : model.prop;
    m4x.name = m4x.name.replace('&', '&amp;');

    m4x.gelink = model.productLink;

    m4x.material = model.material;

    m4x.categories = getModelCategoryXml(model.category);

    if (model.width) {
      m4x.width = buildDimensionString(model.width);
    }

    if (model.height) {
      m4x.height = buildDimensionString(model.height);
    }

    if (model.nodes) {
      m4x.pixelcount = model.nodes;
    }

    let modelExists = false;
    if (model.nativeModel) {
      modelExists = true;
      m4x.notes = `Use Native xLights Model '${model.nativeModel}': ${model.nativeModelSettings}`;
    } else if (model.xmodel.trim() !== '') {
      modelExists = true;
      m4x.xmodelLink = config.downloadFolderURI + model.xmodel;
    }

    if (modelExists) {
      m4x.imageLinks = getImageLinkXml(model.image);

      parsedModels.push(m4x);
    }
  });

  return parsedModels;
};

const getVendorXml = () => {
  const vendorXml = [
    '<vendor>',
    `<name>${config.name}</name>`,
    `<contact>${config.contact}</contact>`,
    `<email>${config.email}</email>`,
    `<website>${config.website}</website>`,
    `<facebook>${config.facebook}</facebook>`,
    `<notes>${config.notes}</notes>`,
    `<logolink><![CDATA[${config.logolink}]]></logolink>`,
    `</vendor>`,
  ];

  return vendorXml.join('\n');
};

const creds = 'dpriem:XmasPixaSPI#10';
let buff = new Buffer(creds);
let base64data = buff.toString('base64');

csv()
  .fromFile(csvFile)
  .then((modelData) => {
    const categoriesXml = createCategories(modelData);
    const parsedModels = parseModels(modelData);
    const vendorInfo = getVendorXml();

    let output = ['<modelinventory>', vendorInfo, '<categories>', categoriesXml, '</categories>', '<models>'];

    parsedModels.sort((a, b) => (a.name > b.name) ? 1 : -1);
    let i = 0;

    parsedModels.forEach((model) => {
      model.id = i;
      const xml = modelXML(model);
      output.push(xml);
      i++;
    });

    output.push('</models>');
    output.push('</modelinventory>');

    const expPath = path.join(__dirname, '../', 'geusa-xlights.xml');
    fs.writeFileSync(expPath, output.join('\n'));

    console.log('\n' + 'geusa-xlights.xml model file updated!');
    console.log('  # of models processed: ' + parsedModels.length);
  });
