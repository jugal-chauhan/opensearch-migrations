function mapToPlainObjectReplacer(key, value) {
    if (value instanceof Map) {
      return Object.fromEntries(value);
    }
    return value;
  }
  
  function transform(document, context) {
    if (!document) {
      throw new Error("No source_document was defined - nothing to transform!");
    }
  
    const indexCommandMap = document.get("index");
    const sourceDocumentMap = document.get("source");
    const baseId = indexCommandMap.get("_id");
    const results = [];
    const N = 5;
  
    for (let i = 0; i <= N; i++) {
      const newIndexMap = new Map(indexCommandMap);
      newIndexMap.set("_id", baseId + i.toString());
      results.push(new Map([
        ["index", newIndexMap],
        ["source", sourceDocumentMap]
      ]));
    }
  
    return results;
  }
  
  function main(context) {
    console.log("Context: ", JSON.stringify(context, mapToPlainObjectReplacer, 2));
    return (document) => {
      if (Array.isArray(document)) {
        return document.flatMap(item => transform(item, context));
      }
      return transform(document, context);
    };
  }
  
  (() => main)();