# PDF Splitting Result

This folder (`splitted_pdfs`) contains PDF files that were auto-split because their origins exceeded ~5MB.

Since GitHub doesn't support lazy-loading partially from huge monolithic files smoothly, these split files can be used.
Other projects reading the PDFs should refer to `split_manifest.json` which defines:
1. `large_files_to_ignore`: A list of original root PDFs. Your game/app should ignore these completely to save bandwidth.
2. `groups`: A mapping where the key is the ignored original PDF, and the value is an array of sub-files (chunks). The chunks must be loaded and rendered as if they were a single document.

Any PDF not listed in `large_files_to_ignore` can be loaded normally.

### How to use via mapping:
```javascript
import manifest from './split_manifest.json';

function getPdfPathsToLoad(originalPath) {
    if (manifest.large_files_to_ignore.includes(originalPath)) {
        return manifest.groups[originalPath]; // array of split parts
    }
    return [originalPath]; // directly return original small file
}
```
